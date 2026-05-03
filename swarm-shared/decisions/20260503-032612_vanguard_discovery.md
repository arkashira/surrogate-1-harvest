# vanguard / discovery

## Final Synthesized Solution

### Diagnosis (merged)
- No CDN-first, content-addressed manifest exists; ingestion/training scripts can still trigger `list_repo_tree`/`load_dataset` at runtime → 429s, quota burn, non-reproducible runs.
- Missing deterministic file list keyed by date/slug; training jobs cannot pin exact data snapshot and must re-query HF API on every run.
- No local cache or JSON manifest committed to repo; each Lightning worker re-enumerates files and risks rate limits during data loader init.
- No clear separation between Mac orchestration (ingest) and Lightning training (train) → brittle reproducibility.
- No lightweight verification that CDN URLs resolve before training starts (wastes quota on failed runs).

### Proposed Change
Create `/opt/axentx/vanguard/discovery/` with three files (incremental, no existing files modified):
- `build_manifest.py` — Mac-side script that lists one date folder via `list_repo_tree`, emits `manifest-{date}.json` with CDN URLs and content-addressed hints (size, etag, sha256 placeholder).
- `verify_cdn.py` — Quick HEAD check of all CDN URLs in a manifest before training starts; fails fast if any URL is broken.
- `train_cdn_only.py` — Lightning training entrypoint that loads the embedded manifest and fetches only via CDN (zero HF API calls during training).

### Implementation

```bash
mkdir -p /opt/axentx/vanguard/discovery
```

#### `/opt/axentx/vanguard/discovery/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Build a content-addressed manifest for a date folder in an HF dataset repo.

Usage (Mac, after rate-limit window clears):
  HF_REPO="datasets/your/repo" python build_manifest.py --date 2026-05-03 --out manifest-2026-05-03.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date: str, out_path: Path):
    prefix = f"{date}/"
    tree = list_repo_tree(repo, recursive=True, path=prefix)
    entries = []
    for item in tree:
        if item.type != "file":
            continue
        path = item.path
        url = CDN_TEMPLATE.format(repo=repo, path=path)
        slug = Path(path).name
        entries.append({
            "slug": slug,
            "path": path,
            "url": url,
            "size": getattr(item, "size", None),
            "etag": getattr(item, "etag", None),
            "sha256": getattr(item, "sha256", None)
        })

    manifest = {
        "repo": repo,
        "date": date,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "count": len(entries),
        "files": entries
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(entries)} entries to {out_path}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=os.getenv("HF_REPO", "datasets/example/dataset"))
    p.add_argument("--date", required=True, help="YYYY-MM-DD folder in repo")
    p.add_argument("--out", required=True, help="Output JSON path")
    args = p.parse_args()
    build_manifest(args.repo, args.date, Path(args.out))
```

#### `/opt/axentx/vanguard/discovery/verify_cdn.py`
```python
#!/usr/bin/env python3
"""
Quick HEAD check for CDN URLs in a manifest.

Usage: python verify_cdn.py manifest-2026-05-03.json
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

def check(url: str, timeout=10):
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        return url, r.status_code, r.headers.get("content-length")
    except Exception as e:
        return url, str(e), None

def main(manifest_path: Path):
    with open(manifest_path, encoding="utf-8") as f:
        m = json.load(f)

    urls = [f["url"] for f in m.get("files", [])]
    ok = 0
    fail = 0

    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(check, u): u for u in urls}
        for fut in as_completed(futures):
            url, status, _ = fut.result()
            if status == 200:
                ok += 1
            else:
                fail += 1
            sys.stdout.write(f"{status} {url}\n")

    print(f"\nOK={ok} FAIL={fail} TOTAL={len(urls)}")
    if fail > 0:
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: verify_cdn.py manifest.json")
        sys.exit(1)
    main(Path(sys.argv[1]))
```

#### `/opt/axentx/vanguard/discovery/train_cdn_only.py`
```python
#!/usr/bin/env python3
"""
Lightning training entrypoint that uses CDN-only file list.
Embed manifest at launch (or mount via shared storage).

Usage (Lightning):
  python train_cdn_only.py --manifest manifest-2026-05-03.json --output-dir ./ckpt
"""
import argparse
import io
import json
from pathlib import Path

import lightning as L
import requests
import torch
from torch.utils.data import DataLoader, IterableDataset


class CDNTextDataset(IterableDataset):
    """
    Iterate files from a manifest and yield parsed records.
    Replace parse_file() with your actual parser (JSONL, parquet, etc.).
    """

    def __init__(self, manifest_path: Path, max_files=None):
        with open(manifest_path, encoding="utf-8") as f:
            self.manifest = json.load(f)
        self.urls = [f["url"] for f in self.manifest["files"]]
        if max_files:
            self.urls = self.urls[:max_files]

    def parse_file(self, content: bytes):
        # Implement per your file format.
        # Example for newline-delimited JSON:
        # for line in content.decode().strip().splitlines():
        #     obj = json.loads(line)
        #     yield {"prompt": obj["prompt"], "response": obj["response"]}
        yield {"prompt": "dummy prompt", "response": "dummy response"}

    def __iter__(self):
        for url in self.urls:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            yield from self.parse_file(resp.content)


class LitModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.net = torch.nn.Linear(128, 128)

    def training_step(self, batch, batch_idx):
        # Replace with real model/step
        loss = torch.tensor(0.0, requires_grad=True)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--output-dir", default="./ckpt")
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--devices", type=int, default=1)
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"
