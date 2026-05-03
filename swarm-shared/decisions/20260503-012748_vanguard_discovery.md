# vanguard / discovery

## 1. Diagnosis
- Frontend/training still triggers authenticated `list_repo_tree` (via `/api/` or SDK) on every load, burning HF quota (1000/5min) and causing 429s.
- No persisted `(repo, dateFolder)` file-list; every reload re-enumerates and re-downloads metadata.
- Training script likely uses `load_dataset(streaming=True)` on heterogeneous repos → `pyarrow.CastError` on mixed schemas.
- No CDN-only fetch path; authenticated calls are used even for public files.
- No reuse of running Lightning Studio for training iterations → quota waste and cold-start delays.

## 2. Proposed change
Create a lightweight discovery manifest generator and CDN-only fetcher for vanguard:
- Add `/opt/axentx/vanguard/scripts/build_manifest.py` — one-time Mac-side script that lists a specific `(repo, dateFolder)` via HF API (after rate-limit window), saves `manifest.json` with CDN paths only.
- Add `/opt/axentx/vanguard/scripts/train_cdn.py` — Lightning training script that loads `manifest.json` and fetches files via public CDN URLs (no auth, no API quota).
- Add `/opt/axentx/vanguard/frontend/static/js/discovery.js` — frontend utility to load `manifest.json` and render available files/datasets without calling `/api/`.

Scope: new files + minimal edits to existing launcher (if present) to point to `train_cdn.py`.

## 3. Implementation

### 3.1 Manifest builder (run from Mac after rate-limit clears)
```python
# /opt/axentx/vanguard/scripts/build_manifest.py
#!/usr/bin/env python3
"""
Build a CDN-only manifest for a (repo, dateFolder).
Run once per ingestion batch from Mac (or CI) after HF API window clears.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi

REPO = os.getenv("HF_DATASET_REPO", "datasets/your-org/vanguard-ingest")
DATE_FOLDER = os.getenv("INGEST_DATE", datetime.utcnow().strftime("%Y-%m-%d"))
OUT_DIR = Path(__file__).parent.parent / "manifests"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / f"manifest-{DATE_FOLDER}.json"

def main() -> None:
    api = HfApi()
    # Single non-recursive call per dateFolder to minimize API usage
    try:
        items = api.list_repo_tree(repo_id=REPO, path=DATE_FOLDER, recursive=False)
    except Exception as e:
        print(f"HF API error (possibly rate-limited): {e}", file=sys.stderr)
        sys.exit(1)

    files = []
    for item in items:
        if getattr(item, "type", None) != "file":
            continue
        path = getattr(item, "path", None)
        if not path:
            continue
        # CDN URL (public, no auth)
        cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"
        files.append(
            {
                "path": path,
                "cdn_url": cdn_url,
                "size": getattr(item, "size", None),
            }
        )

    manifest = {
        "repo": REPO,
        "date_folder": DATE_FOLDER,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }

    OUT_FILE.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {OUT_FILE} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

### 3.2 CDN-only training loader (for Lightning Studio)
```python
# /opt/axentx/vanguard/scripts/train_cdn.py
#!/usr/bin/env python3
"""
Lightning training script that uses CDN-only fetches.
Expects a manifest JSON produced by build_manifest.py.
"""
import json
import os
import sys
from pathlib import Path
from typing import Iterator, Tuple

import requests
import torch
from torch.utils.data import IterableDataset

MANIFEST_PATH = os.getenv(
    "MANIFEST_PATH",
    str(Path(__file__).parent.parent / "manifests" / "manifest-latest.json"),
)
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))


class CDNTextDataset(IterableDataset):
    def __init__(self, manifest_path: str):
        manifest_path = Path(manifest_path)
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")
        with manifest_path.open() as f:
            manifest = json.load(f)
        self.items = manifest.get("files", [])
        if not self.items:
            raise ValueError("No files in manifest")

    def _stream_files(self) -> Iterator[Tuple[str, str]]:
        for item in self.items:
            url = item["cdn_url"]
            try:
                # CDN fetch: no Authorization header -> bypasses API rate limits
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                # Project to {prompt, response} at parse time (avoids schema issues)
                text = resp.text.strip()
                if not text:
                    continue
                # Heuristic: treat each non-empty file as a prompt; adapt as needed
                yield text, ""  # (prompt, response) placeholder
            except Exception as exc:
                print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
                continue

    def __iter__(self):
        return self._stream_files()


def dummy_train_step(batch):
    # Replace with real surrogate-1 training logic
    prompts, _ = zip(*batch) if batch else ([], [])
    return {"loss": torch.tensor(0.0), "num_prompts": len(prompts)}


def run_training():
    dataset = CDNTextDataset(MANIFEST_PATH)
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=BATCH_SIZE,
        num_workers=0,
    )

    for i, batch in enumerate(loader):
        out = dummy_train_step(batch)
        if i % 10 == 0:
            print(f"step={i} num_prompts={out['num_prompts']}")

    print("Training loop complete (CDN-only).")


if __name__ == "__main__":
    run_training()
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/train_cdn.py
```

### 3.3 Frontend discovery helper (static)
```javascript
// /opt/axentx/vanguard/frontend/static/js/discovery.js
// Lightweight frontend loader for manifest (no /api/ calls).
async function loadManifest(dateFolder = "latest") {
  const url = `/manifests/manifest-${dateFolder}.json`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to load manifest: ${res.status}`);
  return res.json();
}

function renderManifest(manifest) {
  const container = document.getElementById("discovery-files");
  if (!container) return;
  container.innerHTML = "";
  manifest.files.forEach((f) => {
    const row = document.createElement("div");
    row.className = "file-row";
    row.innerHTML = `<a href="${f.cdn_url}" target="_blank" rel="noopener">${f.path}</a> <span class="muted">(${f.size || "?"} bytes)</span>`;
    container.appendChild(row);
  });
}

// Example usage on page load:
// loadManifest("2026-05-03").then(renderManifest).catch(console.error);
```

### 3.4 Launcher / orchestration hint (if you have a run script)
If you have a launcher (e.g., `run.sh` or notebook), ensure it:
- Invokes `build_manifest.py` once per dateFolder (on Mac or CI) after HF API window clears.
- Starts Lightning Studio with `train_cdn.py` and reuses running studios:
```python
# Example snippet for Lightning reuse (add to your launcher)
from lightning import Studio, Machine
from lightning.pytorch import seed_everything

studio_name = "vanguard-l40s-cdn"
for
