# vanguard / quality

## 1. Diagnosis

- No persisted `(repo, dateFolder)` file manifest — every training run re-enumerates via authenticated HF API, burning quota and risking 429.
- Training script likely uses `load_dataset(streaming=True)` or recursive enumeration on heterogeneous repos, triggering PyArrow schema errors and rate limits.
- Lightning Studio reuse is not enforced — idle-stop kills training and scripts likely recreate studios instead of reusing running ones, wasting 80+ hrs/mo quota.
- No CDN bypass strategy — data loading still uses authenticated `/api/` paths instead of public CDN `resolve/main/` URLs, keeping rate-limit exposure high.
- Missing deterministic shard-to-repo routing for HF commit cap — all writes target one repo, risking 128/hr cap and ingestion stalls.

## 2. Proposed change

Add a lightweight manifest generator + training launcher that:
- Persists a `manifests/{repo}/{dateFolder}.json` listing only file paths (single non-recursive `list_repo_tree` call per date folder).
- Embeds that manifest in the training script so Lightning workers fetch via CDN URLs only (zero authenticated calls during training).
- Reuses a running Lightning Studio if present; otherwise starts one with deterministic sizing (L40S → fallback).
- Adds shard routing for writes (hash-slug → 1-of-5 sibling repos) to avoid HF commit cap.

Scope:
- Create `/opt/axentx/vanguard/bin/gen_manifest.py`
- Create `/opt/axentx/vanguard/bin/run_surrogate_train.py`
- Touch `/opt/axentx/vanguard/train/train.py` (lightweight CDN-only dataset loader)
- Add `/opt/axentx/vanguard/config/sibling_repos.json`

## 3. Implementation

```bash
# Ensure structure
mkdir -p /opt/axentx/vanguard/{bin,train,config,manifests}
```

### 3.1 config/sibling_repos.json
```json
{
  "repos": [
    "org/surrogate-1-shard-a",
    "org/surrogate-1-shard-b",
    "org/surrogate-1-shard-c",
    "org/surrogate-1-shard-d",
    "org/surrogate-1-shard-e"
  ]
}
```

### 3.2 bin/gen_manifest.py
```python
#!/usr/bin/env python3
"""
Generate a non-recursive file manifest for a repo/dateFolder.
Usage:
  HF_TOKEN=hf_xxx python gen_manifest.py \
    --repo org/surrogate-1 \
    --date 2026-04-29 \
    --out manifests/org/surrogate-1/2026-04-29.json
"""
import argparse
import json
import os
import sys
from pathlib import Path

import huggingface_hub

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate HF repo dateFolder manifest")
    parser.add_argument("--repo", required=True, help="HF repo id (org/repo)")
    parser.add_argument("--date", required=True, help="Date folder under datasets/ (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        print("ERROR: HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    api = huggingface_hub.HfApi(token=token)
    prefix = f"{args.date}/"
    try:
        tree = api.list_repo_tree(
            repo_id=args.repo,
            path=prefix,
            recursive=False,
            repo_type="dataset",
        )
    except Exception as exc:
        print(f"ERROR listing repo tree: {exc}", file=sys.stderr)
        sys.exit(1)

    files = [item.rfilename for item in tree if item.rfilename.endswith((".parquet", ".jsonl", ".json"))]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "repo": args.repo,
        "date": args.date,
        "files": sorted(files),
    }
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files -> {out_path}")

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/bin/gen_manifest.py
```

### 3.3 bin/run_surrogate_train.py
```python
#!/usr/bin/env python3
"""
Lightning-aware training launcher:
- Reuse running Studio if present.
- Embed manifest and use CDN-only fetches.
- Route writes across shards.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import lightning as L

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "train"))

from train import SurrogateTrainer  # assumes train.py exposes SurrogateTrainer

def pick_shard_repo(slug: str, config_path: Path) -> str:
    cfg = json.loads(config_path.read_text())
    repos = cfg["repos"]
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % len(repos)
    return repos[idx]

def find_running_studio(name: str) -> L.studio.Studio | None:
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "running":
            return s
    return None

def main() -> None:
    # Required env
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        print("ERROR: HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    manifest_path = REPO_ROOT / "manifests" / "org/surrogate-1" / "2026-04-29.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest missing: {manifest_path}", file=sys.stderr)
        print("Run: bin/gen_manifest.py ...", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text())
    studio_name = "vanguard-surrogate-1"
    studio = find_running_studio(studio_name)

    if studio is None:
        print("No running studio found; starting L40S (fallback public tier)...")
        # Lightning free tier -> public-prod (L40S). Paid tier would use lambda-prod for H200.
        studio = L.studio.Studio(
            name=studio_name,
            lightning_version="latest",
            machine="L40S",
            cloud_account="lightning-public-prod",
            create_ok=True,
        )
    else:
        print(f"Reusing running studio: {studio_name}")

    # Route output repo by slug to avoid HF commit cap
    slug = f"surrogate-run-{manifest['date']}"
    target_repo = pick_shard_repo(slug, REPO_ROOT / "config" / "sibling_repos.json")
    print(f"Selected shard repo: {target_repo}")

    # Launch training in studio (non-blocking run)
    job = studio.run(
        fn=SurrogateTrainer.run,
        manifest=manifest,
        target_repo=target_repo,
        # Pass CDN base to avoid authenticated API calls during training
        cdn_base="https://huggingface.co/datasets",
        hf_token=hf_token,
    )
    print(f"Launched job: {job}")

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/bin/run_surrogate_train.py
```

### 3.4 train/train.py (lightweight CDN loader)
```python
"""
Minimal training entry that uses CDN-only file fetches.
Expects manifest = {"repo": "...", "date": "...", "files": [...]}
"""
import json
from pathlib import Path
from typing import Dict, List

import torch
from torch.utils.data import IterableDataset, DataLoader
import requests
from tqdm import tqdm

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest: Dict, cdn_base: str = "https://huggingface.co/datasets"):
        super().__init__()
        self.repo = manifest["repo"]
        self.date = manifest["date"]
       
