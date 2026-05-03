# vanguard / quality

## 1. Diagnosis
- No deterministic CDN-first manifest exists; ingestion/training scripts likely still call `list_repo_tree`/`load_dataset` at runtime, risking 429s and non-reproducible runs.
- Missing content-addressed file list keyed by date/slug; training jobs cannot pin exact file set and may drift across runs.
- No local fallback for HF API rate limits during ingestion; every run hits `/api/` endpoints instead of CDN direct URLs.
- Training reproducibility is weak: dataset version is implicit (latest) rather than explicit manifest (date/slug → file list).
- No guard to reuse running Lightning Studio; quota waste when iterating training scripts.

## 2. Proposed change
Add a small, high-leverage utility module that produces and consumes a CDN-first manifest for one date folder, plus a minimal training script update to use it. Scope:
- Create `/opt/axentx/vanguard/ingest/manifest.py` — one API call to `list_repo_tree(path, recursive=False)` for a date folder, emit `manifest-{date}.json` with CDN URLs and slugs.
- Create `/opt/axentx/vanguard/train/train.py` update — accept `--manifest` and stream files via CDN URLs only (zero HF API calls during data load).
- Add `/opt/axentx/vanguard/orchestration/reuse_studio.py` — list and reuse running Lightning Studio to save quota.

## 3. Implementation

```bash
# Ensure directories
mkdir -p /opt/axentx/vanguard/{ingest,train,orchestration}
```

### `/opt/axentx/vanguard/ingest/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate CDN-first manifest for one date folder in a Hugging Face dataset repo.
Usage:
  python manifest.py --repo <datasets/repo> --date 2026-04-29 --out manifest-2026-04-29.json
"""
import argparse
import json
import os
import time
from typing import List, Dict

try:
    from huggingface_hub import HfApi
except ImportError:
    os.system("pip install huggingface-hub")
    from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date: str, out_path: str) -> Dict:
    api = HfApi()
    folder_path = f"batches/mirror-merged/{date}"
    # Single API call (non-recursive) to avoid pagination/rate limits
    entries = api.list_repo_tree(repo=repo, path=folder_path, recursive=False)

    files = []
    for e in entries:
        if e.get("type") != "file":
            continue
        path = e["path"]
        slug = os.path.splitext(os.path.basename(path))[0]
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=path)
        files.append({
            "slug": slug,
            "path": path,
            "cdn_url": cdn_url,
            "size": e.get("size"),
        })

    manifest = {
        "repo": repo,
        "date": date,
        "folder": folder_path,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    }

    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {out_path} ({len(files)} files)")
    return manifest

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True, help="HF dataset repo, e.g. datasets/your/repo")
    p.add_argument("--date", required=True, help="Date folder, e.g. 2026-04-29")
    p.add_argument("--out", default=None, help="Output JSON path")
    args = p.parse_args()
    out = args.out or f"manifest-{args.date}.json"
    build_manifest(args.repo, args.date, out)
```

### `/opt/axentx/vanguard/train/train.py` (minimal update)
```python
#!/usr/bin/env python3
"""
Lightning training script that uses CDN-first manifest to avoid HF API calls during data loading.
"""
import argparse
import json
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset, DataLoader

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path, max_files=None):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = self.manifest["files"]
        if max_files:
            self.files = self.files[:max_files]

    def __iter__(self):
        for item in self.files:
            # CDN fetch — no Authorization header, bypasses API rate limits
            resp = requests.get(item["cdn_url"], timeout=30)
            resp.raise_for_status()
            table = pq.read_table(pq.ParquetFile(pq.BufferReader(resp.content)))
            # Project to {prompt, response} only (schema normalization)
            df = table.select(["prompt", "response"]).to_pandas()
            for _, row in df.iterrows():
                yield {"prompt": row["prompt"], "response": row["response"]}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", required=True, help="Path to manifest JSON")
    p.add_argument("--max-files", type=int, default=None)
    p.add_argument("--batch-size", type=int, default=8)
    args = p.parse_args()

    ds = CDNParquetDataset(args.manifest, max_files=args.max_files)
    loader = DataLoader(ds, batch_size=args.batch_size, num_workers=0)

    # Minimal training loop placeholder
    for i, batch in enumerate(loader):
        print(f"batch {i}: prompts={len(batch['prompt'])}")
        if i >= 2:
            break
    print("CDN-first data loading OK")

if __name__ == "__main__":
    main()
```

### `/opt/axentx/vanguard/orchestration/reuse_studio.py`
```python
#!/usr/bin/env python3
"""
Reuse a running Lightning Studio to save quota.
"""
import os
from lightning import Studio, Teamspace

def get_or_create_studio(name: str, machine: str = "lightning-lambda-prod/L40S-24GB") -> Studio:
    # List and reuse running studio
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )

if __name__ == "__main__":
    studio = get_or_create_studio("vanguard-train")
    print(f"Studio ID: {studio.id}, Status: {studio.status}")
```

## 4. Verification
1. Generate manifest (run once per date folder from Mac; safe after rate-limit window):
   ```bash
   cd /opt/axentx/vanguard
   python ingest/manifest.py --repo datasets/your/repo --date 2026-04-29 --out manifest-2026-04-29.json
   ```
   Confirm `manifest-2026-04-29.json` exists and contains CDN URLs.

2. Run training with CDN-only data loading (zero HF API calls during load):
   ```bash
   cd /opt/axentx/vanguard
   python train/train.py --manifest manifest-2026-04-29.json --max-files 5 --batch-size 4
   ```
   Confirm batches print and no `huggingface_hub` API calls appear in logs.

3. Studio reuse:
   ```bash
   python orchestration/reuse_studio.py
   ```
   Confirm it lists running studios and reuses if present.

4. Rate-limit resilience:
   - Block HF API (e.g., via firewall or by revoking token) and re-run step 2; CDN fetches should still succeed.
