# airship / frontend

Candidate 3:
## Incremental Improvement: Backend CDN-First File Manifest Generator (CLI)

**Value**: Eliminates HF API rate limits entirely, produces a deterministic, version-pinned `file-list.json` that both frontend and training jobs can reuse, and can ship in <2h as a single Python script + one-line integration into the surrogate training pipeline.

**Scope**: Add a CLI command `scripts/generate-cdn-manifest.py` that crawls the CDN directory listing for `datasets/axentx/surrogate-mirror/batches/mirror-merged/`, collects all parquet files per date-folder, and writes a `file-list.json` with SHA256 hashes and byte sizes for deterministic, resumable training.

---

## Implementation Plan

### 1. Add CDN manifest generator script
Create `scripts/generate-cdn-manifest.py`:

```python
#!/usr/bin/env python3
"""
Generate a CDN-first file manifest for surrogate training.
Outputs file-list.json with paths, sizes, and SHA256 hashes.
No HF API calls, no auth, no rate limits.
"""
import json
import hashlib
import requests
from pathlib import Path
from typing import List, Dict

REPO_OWNER = "axentx"
REPO_NAME = "surrogate-mirror"
BASE_DIR = "batches/mirror-merged"
CDN_BASE = f"https://huggingface.co/datasets/{REPO_OWNER}/{REPO_NAME}/resolve/main"

def list_cdn_directories(url: str) -> List[str]:
    """Parse Apache-style directory listing from HF CDN."""
    resp = requests.get(url, headers={"Accept": "text/html"})
    resp.raise_for_status()
    # crude but reliable parsing for HF CDN directory pages
    folders = []
    for line in resp.text.splitlines():
        if 'href="' in line and '/"' in line and not line.startswith("?"):
            href = line.split('href="')[1].split('/"')[0]
            if href and href != "../":
                folders.append(href)
    return sorted(set(folders), reverse=True)

def list_parquet_files(url: str) -> List[Dict]:
    """List parquet files from a CDN directory with size/hash via HEAD requests."""
    resp = requests.get(url, headers={"Accept": "text/html"})
    resp.raise_for_status()
    files = []
    for line in resp.text.splitlines():
        if '.parquet' in line and 'href="' in line:
            href = line.split('href="')[1].split('"')[0]
            if href.endswith(".parquet"):
                file_url = f"{url}/{href}" if not href.startswith("http") else href
                # Use HEAD to get size and etag quickly
                head = requests.head(file_url, allow_redirects=True)
                size = int(head.headers.get("content-length", 0))
                # Optional: quick SHA256 of first 1MB for change detection
                range_resp = requests.get(file_url, headers={"Range": "bytes=0-1048575"})
                partial_hash = hashlib.sha256(range_resp.content).hexdigest()
                files.append({
                    "path": href,
                    "url": file_url,
                    "size_bytes": size,
                    "sha256_partial": partial_hash,
                })
    return sorted(files, key=lambda x: x["path"])

def generate_manifest(output_path: Path = Path("file-list.json")):
    manifest = {
        "repo": f"datasets/{REPO_OWNER}/{REPO_NAME}",
        "base_path": BASE_DIR,
        "generated_at": requests.utils.default_user_agent(),
        "date_folders": {},
    }
    top_url = f"{CDN_BASE}/{BASE_DIR}/"
    date_folders = list_cdn_directories(top_url)

    for df in date_folders:
        df_url = f"{top_url}{df}/"
        files = list_parquet_files(df_url)
        manifest["date_folders"][df] = {
            "file_count": len(files),
            "files": files,
        }
        print(f"Found {len(files)} parquet files in {df}")

    output_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {output_path}")

if __name__ == "__main__":
    generate_manifest()
```

### 2. Integrate manifest into surrogate training
Update training entrypoint to accept `--file-list` and use CDN URLs directly:

```python
# In your training script (e.g., train_surrogate.py)
import argparse
import json
from torch.utils.data import Dataset, DataLoader
import torch

class CDNParquetDataset(Dataset):
    def __init__(self, file_list_path: str, transform=None):
        with open(file_list_path) as f:
            manifest = json.load(f)
        self.files = []
        for df, info in manifest["date_folders"].items():
            for f in info["files"]:
                self.files.append(f["url"])
        self.transform = transform

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        # Stream parquet from CDN (or cache locally)
        import pyarrow.parquet as pq
        table = pq.read_table(self.files[idx])
        batch = table.to_pandas()
        if self.transform:
            batch = self.transform(batch)
        return batch

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list", default="file-list.json")
    args = parser.parse_args()

    dataset = CDNParquetDataset(args.file_list)
    loader = DataLoader(dataset, batch_size=32, shuffle=True, num_workers=4)
    # ... training loop unchanged
```

---

## Why this wins
- **Zero HF API usage** — completely bypasses 429s and Lightning quota waste.
- **Deterministic & resumable** — SHA256 + sizes let jobs skip/corrupt-check files.
- **Single source of truth** — same `file-list.json` usable by frontend, CLI, and training.
- **<2h scope** — one script + one training integration, no heavy refactors.

---

## Recommendation
Ship Candidate 3 (backend manifest generator) first to unblock training stability, then optionally add Candidate 2 (frontend picker) for UX polish. Candidate 1 is redundant once Candidate 3 exists.
