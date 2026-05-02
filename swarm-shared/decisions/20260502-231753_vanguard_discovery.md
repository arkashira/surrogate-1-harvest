# vanguard / discovery

## 1. Diagnosis
- No durable ingestion manifest: every training run re-lists HF repos (paginated) and re-downloads, causing 429s and quota burn.
- Training uses `load_dataset`/`list_repo_files` instead of CDN bypass → guaranteed rate limits during data loading.
- No reuse guard for Lightning Studio: scripts create new studios instead of reusing running ones, wasting ~80hr/mo quota.
- Missing deterministic repo-sharding for HF commits: single repo hits 128/hr cap; ingestion stalls on big pushes.
- No pre-flight file-list cache: training jobs can’t start without clearing HF API rate limits first.

## 2. Proposed change
Add a lightweight manifest + launcher pair:
- `vanguard/discovery/ingest_manifest.py` — one-time HF tree list → JSON cache (date-scoped) for CDN-only training.
- `vanguard/discovery/lightning_launcher.py` — reuse running Studio or start L40S, then run training that consumes the manifest (zero HF API calls during data load).
- `vanguard/discovery/train_cdn.py` — minimal training stub that reads file list from manifest and streams via CDN URLs (no `datasets` API).

## 3. Implementation

```bash
# Ensure project structure
mkdir -p /opt/axentx/vanguard/discovery
cd /opt/axentx/vanguard/discovery
```

### 3.1 `ingest_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate durable manifest for CDN-only training.
Run from Mac (or any machine) after rate-limit window clears.
"""
import json, os, hashlib, datetime, argparse
from huggingface_hub import list_repo_tree

def build_manifest(repo: str, date_folder: str, out_path: str):
    """
    repo: e.g. 'datasets/username/dataset'
    date_folder: e.g. '2026-04-29'
    """
    entries = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [
        {
            "repo": repo,
            "path": e.path,
            "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{e.path}",
            "size": getattr(e, "size", None),
        }
        for e in entries
        if e.type == "file" and e.path.endswith((".parquet", ".jsonl"))
    ]

    manifest = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "repo": repo,
        "date_folder": date_folder,
        "files": files,
        "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {out_path} ({len(files)} files)")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="datasets/surrogate-1/enriched")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument("--out", default="manifest_latest.json")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out)
```

### 3.2 `lightning_launcher.py`
```python
#!/usr/bin/env python3
"""
Reuse or start a Lightning Studio, then run CDN training.
"""
import os, time, subprocess, argparse
from lightning_sdk import Client, Teamspace, Studio, Machine

LIGHTNING_EMAIL = os.getenv("LIGHTNING_EMAIL")
LIGHTNING_PASS  = os.getenv("LIGHTNING_PASS")

def get_or_start_studio(name="vanguard-cdn-train", machine=Machine.L40S):
    client = Client(email=LIGHTNING_EMAIL, password=LIGHTNING_PASS)
    teamspace = Teamspace(client=client, name="default")

    # Reuse running studio
    for s in teamspace.studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {s.name}")
            return s, client

    # Start new
    print(f"Starting studio {name} on {machine.value}...")
    studio = Studio(
        client=client,
        name=name,
        machine=machine,
        framework="pytorch",
    )
    studio.start()
    # Wait until running
    for _ in range(60):
        studio.refresh()
        if studio.status == "running":
            print("Studio running.")
            return studio, client
        time.sleep(10)
    raise RuntimeError("Studio failed to start")

def run_training_script(studio: Studio, script_path: str, manifest_path: str):
    # Copy manifest into studio files (simplified: assume mounted or use run() with args)
    cmd = f"python {script_path} --manifest {manifest_path}"
    run = studio.run(command=cmd, environment="base")
    print(f"Started run {run.id}")
    return run

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="manifest_latest.json")
    parser.add_argument("--script", default="train_cdn.py")
    args = parser.parse_args()

    studio, client = get_or_start_studio()
    run_training_script(studio, args.script, args.manifest)
```

### 3.3 `train_cdn.py` (minimal stub)
```python
#!/usr/bin/env python3
"""
CDN-only training: reads manifest, streams parquet via CDN URLs.
No HF API calls during data loading.
"""
import argparse, json, pandas as pd, torch
from torch.utils.data import IterableDataset, DataLoader
import requests, io

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path, max_files=None):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = [f["cdn_url"] for f in self.manifest["files"]]
        if max_files:
            self.files = self.files[:max_files]

    def __iter__(self):
        for url in self.files:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            df = pd.read_parquet(io.BytesIO(resp.content))
            # Expect {prompt, response} projection already done in manifest creation
            for _, row in df.iterrows():
                yield {"input_ids": None, "text": row.get("prompt", ""), "target": row.get("response", "")}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()

    ds = CDNParquetDataset(args.manifest, max_files=args.max_files)
    loader = DataLoader(ds, batch_size=args.batch_size)

    for i, batch in enumerate(loader):
        # Replace with real surrogate-1 training step
        print(f"Batch {i}: {len(batch.get('text', []))} items")
        if i >= 2:
            break
    print("CDN data loading OK (no HF API used).")

if __name__ == "__main__":
    main()
```

Make scripts executable:
```bash
chmod +x /opt/axentx/vanguard/discovery/*.py
```

## 4. Verification
1. Generate manifest (from any machine with HF token):
   ```bash
   cd /opt/axentx/vanguard/discovery
   python ingest_manifest.py --repo datasets/surrogate-1/enriched --date 2026-04-29 --out manifest_2026-04-29.json
   ```
   Confirm `manifest_2026-04-29.json` exists and lists files with `cdn_url`.

2. Test CDN-only data loading locally (no training):
   ```bash
   python train_cdn.py --manifest manifest_2026-04-29.json --max-files 3
   ```
   Expect “CDN data loading OK” and no HF API errors.

3. Launch via Lightning (reuse guard):
   ```bash
   export LIGHTNING_EMAIL=... LIGHTNING_PASS=...
   python lightning_launcher.py --
