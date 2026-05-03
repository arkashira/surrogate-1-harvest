# vanguard / quality

## Final synthesized solution (best parts, contradictions resolved)

**Core decision**: Use a single, canonical manifest that is content-addressed and includes both integrity (sha256) and CDN URLs. Training must use CDN-only paths with zero HuggingFace API calls after manifest creation. Schema enforcement (project to `{prompt,response}`) and deterministic file ordering are mandatory for reproducibility.

---

## 1. Diagnosis (consensus)
- No content-addressed manifest → runtime `list_repo_tree`/`load_dataset` → HF API 429.
- Data path not pinned → non-reproducible runs.
- `enriched/` mixed-schema parquet breaks surrogate-1 expectations (schema errors).
- No CDN-only data path → authenticated API calls during training (rate-limit + latency).
- No deterministic snapshot selection UI/CLI → teams can’t reliably reproduce runs.

---

## 2. Proposed change (unified)
Create:
- `/opt/axentx/vanguard/manifest.py` (CLI + Python API)
- `/opt/axentx/vanguard/manifest/` (folder for manifests)
- `/opt/axentx/vanguard/train/cdn_dataloader.py` (CDN-only dataloader)
- Patch `/opt/axentx/vanguard/train/train.py` to accept `--manifest`
- Update `/opt/axentx/vanguard/README.md` with run instructions

Goal: one command produces `manifest/{date}.json` containing `{path, sha256, cdn_url}`; training uses only CDN URLs with zero API calls after manifest creation.

---

## 3. Implementation (final)

### 3.1 manifest.py (CLI + API)
```python
#!/usr/bin/env python3
"""
Build a content-addressed manifest for a mirror-merged date folder.
Usage:
  HF_REPO=<user>/<dataset> python manifest.py 2026-05-01

Outputs:
  manifest/2026-05-01.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("pip install huggingface_hub")
    sys.exit(1)

HF_REPO = os.getenv("HF_REPO")
if not HF_REPO:
    print("HF_REPO env var required")
    sys.exit(1)

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(128 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(date_str: str, output_dir: Path, repo: str, folder_prefix: str = "batches/mirror-merged"):
    api = HfApi()
    folder = f"{folder_prefix}/{date_str}"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{date_str}.json"

    # List parquet files (non-recursive)
    items = api.list_repo_tree(repo=repo, path=folder, repo_type="dataset", recursive=False)
    files = sorted(
        (it for it in items if it.rfilename.endswith(".parquet")),
        key=lambda x: x.rfilename
    )

    file_entries = []
    for f in files:
        local_path = api.hf_hub_download(repo_id=repo, filename=f.rfilename, repo_type="dataset")
        entry = {
            "path": f.rfilename,
            "size": os.path.getsize(local_path),
            "sha256": sha256_file(local_path),
            "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{f.rfilename}"
        }
        file_entries.append(entry)

    # Snapshot ID = sha256 of canonical file list + sizes
    list_blob = "\n".join(f"{e['path']}\t{e['size']}" for e in file_entries).encode()
    snapshot_id = hashlib.sha256(list_blob).hexdigest()

    manifest = {
        "date": date_str,
        "snapshot_id": snapshot_id,
        "dataset": repo,
        "folder": folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main",
        "files": file_entries,
    }

    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(file_entries)} files -> {out_path}")
    return out_path

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate content-addressed manifest for HF dataset folder.")
    ap.add_argument("date", help="Date folder (YYYY-MM-DD)")
    ap.add_argument("--output-dir", default="manifest", help="Output directory (default: manifest/)")
    ap.add_argument("--repo", default=HF_REPO, help="HF dataset repo (overrides HF_REPO env)")
    ap.add_argument("--folder-prefix", default="batches/mirror-merged", help="Folder prefix inside dataset")
    args = ap.parse_args()

    repo = args.repo or HF_REPO
    if not repo:
        print("Provide repo via HF_REPO env or --repo")
        sys.exit(1)

    build_manifest(args.date, Path(args.output_dir), repo, args.folder_prefix)
```

---

### 3.2 train/cdn_dataloader.py (final)
```python
import io
import json
from typing import Iterator, Tuple

import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset


class CDNParquetIterable(IterableDataset):
    """
    Zero-HF-API dataloader: reads parquet shards directly from CDN URLs.
    Manifest schema:
      {
        "date": "...",
        "snapshot_id": "...",
        "files": [
          {"path": "...", "size": 123, "sha256": "...", "cdn_url": "..."},
          ...
        ]
      }
    Projects columns to (prompt, response) and validates schema.
    """

    def __init__(self, manifest_path: str, columns: Tuple[str, str] = ("prompt", "response")):
        self.manifest_path = manifest_path
        self.columns = columns
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.urls = [e["cdn_url"] for e in self.manifest.get("files", [])]

    def _stream_shard(self, url: str):
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        tbl = pq.read_table(io.BytesIO(resp.content), columns=self.columns)
        # Enforce expected schema
        tbl = tbl.select(self.columns)
        return tbl.to_pylist()

    def __iter__(self) -> Iterator[dict]:
        for url in self.urls:
            try:
                rows = self._stream_shard(url)
                for row in rows:
                    yield row
            except Exception as exc:
                # Skip corrupt shard but log
                print(f"Skipping {url}: {exc}")
                continue
```

---

### 3.3 train/train.py (patch)
```diff
+ import argparse
+ from vanguard.train.cdn_dataloader import CDNParquetIterable
+ from torch.utils.data import DataLoader

-def get_dataloader():
-    # old: runtime HF API calls
-    from datasets import load_dataset
-    ds = load_dataset("user/repo", split="train", streaming=True)
-    return ds

+ def get_dataloader(manifest_path: str, batch_size: int = 8, num_workers: int = 4):
+     ds = CDNParquetIterable(manifest_path, columns=("prompt", "response"))
+     return DataLoader(ds, batch_size=batch_size, num_workers=num_workers)

 if __name__ == "__main__":
+    ap = argparse.ArgumentParser()
+    ap.add_argument("--manifest", required=True, help="Path to manifest/YYYY-MM-DD.json")
+    ap.add_argument("--batch-size", type=int, default=8)
+    ap.add_argument("--num-workers", type=int, default=4)
+    args = ap.parse_args()
+
+    loader = get_dataloader(args.manifest, batch_size=args.batch_size, num_workers=args.num
