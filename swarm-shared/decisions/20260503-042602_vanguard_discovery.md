# vanguard / discovery

## Final Synthesis (adopt this plan)

**Core problem**: training and ingestion re-enumerate repos at runtime, causing HF API 429s, non-reproducible epochs, and inability to do CDN-only fetches.  
**Root cause**: no content-addressed manifest (file list + SHA256) per date folder; ingestion/training re-list repos and write mixed-schema columns that break surrogate-1 expectations.

**Goal**: eliminate HF API calls during training data loading, guarantee reproducible epochs, enable safe resume/restart, and keep surrogate-1 schema clean.

---

## 1. Concrete change (new files only)

Create `/opt/axentx/vanguard/discovery/` with three artifacts:

1. `snapshot_manifest.py` — one-shot Mac orchestration: deterministic, paginated `list_repo_tree` per date folder → JSON manifest `{repo, date_folder, generated_by, files: [{path, sha256, size, url}]}`.
2. `train_cdn_only.py` — Lightning training script that reads the manifest and downloads via CDN URLs only (zero HF API calls during data load). Includes schema guard to reject extra columns and produce strict surrogate-1 `{prompt, response}` pairs.
3. `reuse_studio.sh` — executable Bash wrapper that finds or starts a running L40S Studio and submits `train_cdn_only.py`.

No edits to existing code.

---

## 2. Implementation (merged best parts + fixes)

```bash
mkdir -p /opt/axentx/vanguard/discovery
cd /opt/axentx/vanguard/discovery
```

### snapshot_manifest.py
```python
#!/usr/bin/env python3
"""
Create a content-addressed manifest for CDN-only training.

Usage:
  HF_TOKEN=hf_xxx python snapshot_manifest.py \
    --repo my-org/dataset-repo \
    --date-folder 2026-05-03 \
    --out manifest_2026-05-03.json

Produces reproducible, content-addressed manifest.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import requests
from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_folder(api: HfApi, repo: str, date_folder: str) -> List[str]:
    """Non-recursive, paginated list for one date folder."""
    items: List[str] = []
    page = None
    while True:
        result = api.list_repo_tree(
            repo_id=repo,
            path=date_folder,
            repo_type="dataset",
            pagination=page,
        )
        # Support both list and paginated response
        if isinstance(result, list):
            items.extend([p.rpath for p in result if not p.rpath.endswith("/")])
            break
        items.extend([p.rpath for p in result if not p.rpath.endswith("/")])
        if not getattr(result, "hasNext", False):
            break
        page = getattr(result, "nextPagination", None)
        time.sleep(0.2)
    return items

def sha256_url(url: str) -> str:
    r = requests.get(url, stream=True, timeout=30)
    r.raise_for_status()
    h = hashlib.sha256()
    for chunk in r.iter_content(chunk_size=8192):
        h.update(chunk)
    return h.hexdigest()

def size_from_headers(url: str) -> int:
    r = requests.head(url, timeout=10)
    r.raise_for_status()
    return int(r.headers.get("content-length", 0))

def build_manifest(repo: str, date_folder: str, out_path: Path, include_sha256: bool = True) -> Dict:
    api = HfApi(token=os.getenv("HF_TOKEN"))
    files = list_date_folder(api, repo, date_folder)
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_by": "snapshot_manifest.py",
        "files": [],
    }
    for i, f in enumerate(files):
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=f)
        entry = {
            "path": f,
            "url": cdn_url,
            "size": size_from_headers(cdn_url),
        }
        if include_sha256:
            entry["sha256"] = sha256_url(cdn_url)
        manifest["files"].append(entry)
        if (i + 1) % 50 == 0:
            print(f"Processed {i + 1}/{len(files)}")
        time.sleep(0.05)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} entries to {out_path}")
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Create CDN-only manifest")
    parser.add_argument("--repo", required=True, help="dataset repo id")
    parser.add_argument("--date-folder", required=True, help="date folder path in repo")
    parser.add_argument("--out", default="manifest.json", help="output JSON path")
    parser.add_argument("--skip-sha256", action="store_true", help="skip SHA256 to speed up")
    args = parser.parse_args()

    if not os.getenv("HF_TOKEN"):
        print("Warning: HF_TOKEN not set; public repos may still work.", file=sys.stderr)

    build_manifest(args.repo, args.date_folder, Path(args.out), include_sha256=not args.skip_sha256)

if __name__ == "__main__":
    main()
```

### train_cdn_only.py
```python
#!/usr/bin/env python3
"""
Lightning CDN-only training data loader.

Usage:
  python train_cdn_only.py --manifest manifest_2026-05-03.json --batch-size 16
"""
import argparse
import json
from pathlib import Path
from typing import Iterator, Tuple

import lightning as L
import torch
from torch.utils.data import IterableDataset, DataLoader
import requests

class CDNTextDataset(IterableDataset):
    def __init__(self, manifest_path: str, max_files: int = -1):
        manifest = json.loads(Path(manifest_path).read_text())
        self.entries = manifest["files"][:max_files] if max_files > 0 else manifest["files"]
        self.repo = manifest["repo"]

    def _stream_one(self, url: str) -> str:
        r = requests.get(url, stream=True, timeout=30)
        r.raise_for_status()
        return r.content.decode("utf-8", errors="replace")

    def _parse_to_prompt_response(self, text: str) -> Tuple[str, str]:
        # Strict surrogate-1 projection: no extra metadata columns.
        # Reject lines that look like injected metadata.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        clean_lines = [ln for ln in lines if not ln.startswith(("source:", "ts:", "source =", "ts ="))]
        if not clean_lines:
            return "", ""
        # Prefer first non-empty line as prompt, remainder as response
        prompt = clean_lines[0]
        response = "\n".join(clean_lines[1:]) if len(clean_lines) > 1 else ""
        return prompt.strip(), response.strip()

    def __iter__(self) -> Iterator[Tuple[str, str]]:
        for e in self.entries:
            try:
                txt = self._stream_one(e["url"])
                prompt, response = self._parse_to_prompt_response(txt)
                if prompt:
                    yield prompt, response
            except Exception:
                # skip corrupt/unreadable files; log in production
                continue

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, manifest_path: str, batch_size: int = 16, max_files: int = -1):
        super().__init__()
        self.manifest_path = manifest_path
        self.batch_size = batch_size
        self.max_files = max_files

    def train_dataloader(self):
        ds = CDNTextDataset(self.manifest_path, max_files=self.max_files)
        return DataLoader(ds, batch_size=self.batch_size, num_workers=0)

class SurrogateTrainer(L.LightningModule):
    def __init__(self, lr: float =
