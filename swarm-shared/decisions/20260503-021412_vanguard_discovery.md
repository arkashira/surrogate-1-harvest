# vanguard / discovery

## Final Synthesized Implementation (Best of Both Candidates)

### 1. Diagnosis (Consolidated)
- **No persisted manifest**: Every training run triggers authenticated `list_repo_tree` calls, burning HF API quota and risking 429s.
- **Schema mismatch risk**: `load_dataset(..., streaming=True)` on heterogeneous repos causes `pyarrow` CastErrors when mixed schemas appear.
- **No CDN-only ingestion**: Authenticated API calls are used instead of public CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`), wasting rate-limit budget.
- **No deterministic sharding**: Commits concentrated on one repo can hit the 128/hr cap, blocking ingestion pipelines.
- **No Studio reuse guard**: Training scripts may recreate Lightning Studios instead of reusing running ones, wasting ~80hr/mo quota.

### 2. Proposed Change
Add a lightweight discovery manifest generator and CDN-based training file list for one date folder, with schema projection and Studio reuse.

**Scope**: Single date folder (e.g., `2026-05-03`) for one dataset repo to prove the pattern.

### 3. Implementation

#### 3.1 Create manifest generator (run once from orchestration host/Mac)
```python
# /opt/axentx/vanguard/scripts/gen_filelist.py
#!/usr/bin/env python3
"""
Generate (repo, dateFolder) -> file-list manifest for CDN-only training.
Run from orchestration host after HF API rate-limit window clears.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-05-03")
OUT_PATH = Path(__file__).parent.parent / "manifests" / f"{HF_REPO.split('/')[-1]}_{DATE_FOLDER}.json"

def main() -> None:
    api = HfApi()
    # Single non-recursive call per folder to minimize API usage
    entries = api.list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_FOLDER,
        repo_type="dataset",
        recursive=False,
    )

    files = [
        f"{DATE_FOLDER}/{e.path.rpartition('/')[-1]}"
        for e in entries
        if e.rfilename.endswith(".parquet")
    ]

    manifest = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
        "total_files": len(files),
        # CDN-only URLs (no auth)
        "cdn_base": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main",
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {OUT_PATH} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/gen_filelist.py
```

#### 3.2 Add config
```json
// /opt/axentx/vanguard/config/training.json
{
  "dataset": {
    "hf_repo": "axentx/surrogate-mirror",
    "date_folder": "2026-05-03",
    "manifest_path": "manifests/surrogate-mirror_2026-05-03.json",
    "cdn_only": true
  },
  "lightning": {
    "reuse_running_studio": true,
    "preferred_clouds": ["lightning-lambda-prod", "lightning-public-prod"],
    "machine": "L40S"
  },
  "hf_shard": {
    "enabled": true,
    "sibling_repos": [
      "axentx/surrogate-mirror",
      "axentx/surrogate-mirror-1",
      "axentx/surrogate-mirror-2",
      "axentx/surrogate-mirror-3",
      "axentx/surrogate-mirror-4"
    ]
  }
}
```

#### 3.3 Update training script to use CDN-only file list
```python
# /opt/axentx/vanguard/train/train.py  (partial diff)
import json
import os
from pathlib import Path

import torch
from datasets import load_dataset
from huggingface_hub import HfApi

def load_filelist_from_manifest(manifest_path: str):
    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest missing: {manifest_path}")
    with manifest_path.open() as f:
        m = json.load(f)
    return m["cdn_base"], m["files"]

def get_dataloader(cfg):
    cdn_base, files = load_filelist_from_manifest(cfg["dataset"]["manifest_path"])

    # Build local file map: download once per file via CDN (no auth/rate-limit)
    local_paths = []
    cache_dir = Path("./.cache/hf_cdn")
    cache_dir.mkdir(parents=True, exist_ok=True)

    import urllib.request
    for rel in files:
        url = f"{cdn_base}/{rel}"
        out = cache_dir / rel.replace("/", "_")
        if not out.is_file():
            print(f"Downloading {url} -> {out}")
            urllib.request.urlretrieve(url, out)
        local_paths.append(str(out))

    # Load only local parquet files; project schema at parse time
    dataset = load_dataset(
        "parquet",
        data_files={"train": local_paths},
        split="train",
    )

    # Project to {prompt, response} only (handles mixed upstream schemas)
    def project(example):
        return {
            "prompt": example.get("prompt") or example.get("input") or "",
            "response": example.get("response") or example.get("output") or "",
        }

    dataset = dataset.map(project, remove_columns=dataset.column_names)
    # Continue with tokenization / dataloader...
```

#### 3.4 Studio reuse helper (lightning launcher snippet)
```python
# /opt/axentx/vanguard/train/lightning_launcher.py
from lightning import Lightning, Teamspace

def get_or_create_studio(name: str, machine: str = "L40S"):
    ls = Lightning()
    studios = Teamspace.studios()
    for s in studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return ls.Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )
```

### 4. Verification

1. **Generate manifest** (orchestration host):
   ```bash
   export HF_DATASET_REPO=axentx/surrogate-mirror
   export DATE_FOLDER=2026-05-03
   python3 /opt/axentx/vanguard/scripts/gen_filelist.py
   ```
   Confirm: `manifests/surrogate-mirror_2026-05-03.json` exists with `files` list and `cdn_base`.

2. **Dry-run training loader** (local, no GPU):
   ```bash
   cd /opt/axentx/vanguard
   python3 -c "
   from train.train import get_dataloader
   import json
   cfg = json.load(open('config/training.json'))
   dl = get_dataloader(cfg)
   batch = next(iter(dl))
   print('batch keys:', batch.keys())
   print('prompt sample:', batch['prompt'][0][:80])
   "
   ```
   Confirm: no CastErrors, no auth prompts, and correct batch structure.
