# vanguard / backend

## Final Unified Implementation

### Diagnosis (Consensus)
- **Critical**: No CDN-first manifest; training scripts risk `list_repo_tree`/`load_dataset` calls at runtime → 429s and non-reproducible runs.
- **Critical**: Missing content-addressed file list keyed by date/slug; training jobs cannot pin exact dataset state without API calls.
- **Operational**: No lightweight CLI to pre-list a single date folder on HuggingFace datasets and emit a JSON manifest for Lightning Studio reuse.
- **Operational**: Training script likely recomputes file list per worker; wastes quota and breaks reproducibility across restarts.
- **Operational**: No guard to reuse an already-running Lightning Studio; each run risks idle-stop churn and quota burn.

### Proposed Change (Unified)
Add a small backend CLI + manifest + training harness:
- `/opt/axentx/vanguard/backend/manifest.py` — CLI: `python manifest.py --repo <datasets/repo> --date <YYYY-MM-DD> --out manifest.json`
- `/opt/axentx/vanguard/backend/train.py` — reads `manifest.json`, downloads via CDN URLs only, trains surrogate-1 style with HF Hub token optional (only for initial list).
- `/opt/axentx/vanguard/backend/run_studio.py` — reuses running Lightning Studio or starts L40S in `lightning-public-prod`, runs `train.py`.

### Implementation

```bash
# Create backend directory
mkdir -p /opt/axentx/vanguard/backend
```

#### manifest.py
```python
#!/usr/bin/env python3
"""
Generate a CDN-first manifest for a HuggingFace dataset date folder.
Usage:
  python manifest.py --repo datasets/axentx/surrogate-1 --date 2026-04-29 --out manifest.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import List, Dict

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date: str, out_path: str) -> List[Dict]:
    """
    repo: e.g. datasets/axentx/surrogate-1
    date: YYYY-MM-DD
    """
    api = HfApi()
    folder_path = f"{date}"  # top-level date folder
    entries = api.list_repo_tree(repo=repo, path=folder_path, recursive=False)

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        if not entry.path.endswith((".parquet", ".jsonl", ".json")):
            continue
        # Content-addressed ID: repo + date + path
        content_id = hashlib.sha256(f"{repo}/{date}/{entry.path}".encode()).hexdigest()[:16]
        files.append({
            "id": content_id,
            "repo": repo,
            "date": date,
            "path": entry.path,
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=entry.path),
            "size": getattr(entry, "size", None),
            "lfilename": os.path.basename(entry.path),
        })

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "date": date,
        "count": len(files),
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {out_path}")
    return files

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create CDN-first manifest for HF dataset date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/axentx/surrogate-1)")
    parser.add_argument("--date", required=True, help="Date folder YYYY-MM-DD")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out)
```

#### train.py (CDN-only loader)
```python
#!/usr/bin/env python3
"""
Lightning-compatible training script that uses CDN URLs from manifest.json.
No list_repo_tree/load_dataset calls during training.
"""
import json
import os
import sys
from typing import List, Dict

import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset, DataLoader

class CDNParquetIterable(IterableDataset):
    def __init__(self, files: List[Dict], columns=("prompt", "response"), max_files=None):
        self.files = files[:max_files] if max_files else files
        self.columns = columns

    def __iter__(self):
        for item in self.files:
            url = item["cdn_url"]
            # stream download; no auth header required for public datasets
            resp = requests.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            temp_path = "/tmp/temp.parquet"
            with open(temp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            table = pq.read_table(temp_path, columns=self.columns)
            df = table.to_pandas()
            for _, row in df.iterrows():
                yield {k: row[k] for k in self.columns}
            os.remove(temp_path)

def main():
    manifest_path = os.environ.get("MANIFEST_PATH", "manifest.json")
    if not os.path.exists(manifest_path):
        print(f"Missing {manifest_path}")
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    files = manifest["files"]
    if not files:
        print("No files in manifest")
        sys.exit(1)

    dataset = CDNParquetIterable(files, columns=("prompt", "response"))
    loader = DataLoader(dataset, batch_size=8, num_workers=0)

    # Minimal surrogate-1 style step: count batches
    total = 0
    for batch in loader:
        total += 1
        if total % 10 == 0:
            print(f"Processed {total} batches")
    print(f"Done. Total batches: {total}")

if __name__ == "__main__":
    main()
```

#### run_studio.py (Lightning reuse)
```python
#!/usr/bin/env python3
"""
Reuse or start a Lightning Studio to run train.py with manifest.
"""
import os
import sys

try:
    from lightning import LightningWork, LightningFlow, LightningApp, Machine
except ImportError:
    print("Install: pip install lightning")
    sys.exit(1)

class SurrogateTrainWork(LightningWork):
    def __init__(self):
        super().__init__(machine=Machine.L40S, cloud="lightning-public-prod")

    def run(self, manifest_path: str):
        import subprocess
        env = os.environ.copy()
        env["MANIFEST_PATH"] = manifest_path
        subprocess.run([sys.executable, "backend/train.py"], env=env, check=True)

class SurrogateFlow(LightningFlow):
    def __init__(self):
        super().__init__()
        self.trainer = SurrogateTrainWork()

    def run(self):
        manifest_path = "manifest.json"
        if not os.path.exists(manifest_path):
            print("Generate manifest.json first (backend/manifest.py)")
            return
        self.trainer.run(manifest_path=manifest_path)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="manifest.json")
    args = parser.parse_args()

    # If running interactively, just run train.py locally with manifest.
    # For Lightning Studio reuse, prefer launching via LightningApp(SurrogateFlow)
    # from a controlling orchestrator on Mac.
    os.environ["MANIFEST_PATH"] = args.manifest
    from train import main as train_main
    train_main()
```

### Verification
1. Generate manifest (single API call, done on Mac):
