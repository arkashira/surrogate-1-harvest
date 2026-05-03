# airship / frontend

## Final Synthesized Implementation (Best of Both Candidates)

**Goal**: Eliminate HF API 429s and Lightning quota waste during Surrogate training by implementing **CDN-first deterministic ingestion + Lightning Studio reuse**.

**Why this ships fastest**:  
- Pure orchestration change (no model retraining)  
- Single-file additions: `scripts/generate_manifest.py`, `training/train_cdn.py`, `scripts/launch_training.py`  
- Reuses existing Lightning SDK + HF CDN (no new infra)  
- Cuts HF API calls from O(n) per epoch → O(1) per dataset version  

---

## Implementation Plan

### 1. Pre-list HF file paths once (Mac orchestration)
```python
# /opt/axentx/airship/surrogate/scripts/generate_manifest.py
#!/usr/bin/env python3
"""
Run on Mac after rate-limit window clears.
Pre-lists HF dataset files once → embeds CDN URLs in training script.
"""
import json, os
from huggingface_hub import HfApi

HF_REPO = "axentx/surrogate-dataset-mirror"
DATE_FOLDER = "batches/mirror-merged/2026-05-03"  # or latest
OUTPUT = "/opt/axentx/airship/surrogate/training/file_manifest.json"

api = HfApi()
tree = api.list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=True)
files = [
    {
        "filename": f.rfilename,
        "cdn_url": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{f.rfilename}"
    }
    for f in tree if f.rfilename.endswith(".parquet")
]

os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
with open(OUTPUT, "w") as f:
    json.dump({"date": DATE_FOLDER, "files": files, "count": len(files)}, f, indent=2)

print(f"Manifest saved: {len(files)} files")
```

```bash
chmod +x /opt/axentx/airship/surrogate/scripts/generate_manifest.py
```

### 2. CDN-only DataLoader (zero API calls during training)
```python
# /opt/axentx/airship/surrogate/training/train_cdn.py
import json, os, pyarrow.parquet as pq
import torch
from torch.utils.data import Dataset, DataLoader
from huggingface_hub import hf_hub_download

HF_REPO = "axentx/surrogate-dataset-mirror"

class CDNParquetDataset(Dataset):
    def __init__(self, filelist_json):
        with open(filelist_json) as f:
            self.files = json.load(f)
        # CDN base (no auth, bypasses /api/ rate limits)
        self.cdn_base = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        path = self.files[idx]
        # Local cache via hf_hub_download (uses CDN, no API token checks)
        local = hf_hub_download(
            repo_id=HF_REPO,
            filename=path,
            repo_type="dataset",
            cache_dir=".cache/hf_cdn"
        )
        tbl = pq.read_table(local, columns=["prompt", "response"])
        # Convert to tensors (simplified)
        return {
            "prompt": str(tbl["prompt"][0].as_py()),
            "response": str(tbl["response"][0].as_py())
        }

if __name__ == "__main__":
    # Find latest filelist
    lists = sorted([f for f in os.listdir("training") if f.startswith("filelist-") and f.endswith(".json")])
    if not lists:
        raise FileNotFoundError("No filelist found. Run generate_manifest.py first.")
    latest = os.path.join("training", lists[-1])
    
    dataset = CDNParquetDataset(latest)
    loader = DataLoader(dataset, batch_size=8, shuffle=True)
    
    for batch in loader:
        print(batch["prompt"][0][:100])
```

### 3. Lightning Studio reuse + idle-resume wrapper
```python
# /opt/axentx/airship/surrogate/scripts/launch_training.py
#!/usr/bin/env python3
import os
from lightning_sdk import Teamspace, Studio, Machine

TEAMSPACE = "surrogate-team"
STUDIO_NAME = "surrogate-train-l40s"
MANIFEST = "/opt/axentx/airship/surrogate/training/file_manifest.json"

# Ensure manifest exists (run generator first if missing)
assert os.path.exists(MANIFEST), f"Run generate_manifest.py first: {MANIFEST}"

ts = Teamspace(TEAMSPACE)

# Reuse running studio (saves 80hr/mo quota)
running = [s for s in ts.studios if s.name == STUDIO_NAME and s.status == "Running"]
if running:
    studio = running[0]
    print(f"Reusing running studio: {studio.id}")
else:
    studio = Studio.create(
        teamspace=TEAMSPACE,
        name=STUDIO_NAME,
        machine=Machine.L40S,
        framework="pytorch",
    )
    print(f"Created studio: {studio.id}")

# Lightning idle-stop kills training → restart if stopped
if studio.status != "Running":
    studio.start(machine=Machine.L40S)

# Run training (zero HF API calls during data load)
job = studio.run(
    command="python /workspace/train_cdn.py",
    sync=False,
)
print(f"Training job submitted: {job.id}")
```

```bash
chmod +x /opt/axentx/airship/surrogate/scripts/launch_training.py
```

### 4. Crontab entry (Mac orchestration host)
```bash
# Regenerate manifest daily at 02:00 (after rate-limit window)
SHELL=/bin/bash
0 2 * * * /usr/bin/env bash /opt/axentx/airship/surrogate/scripts/generate_manifest.py >> /var/log/airship_manifest.log 2>&1
```

---

## Verification

```bash
# 1. Generate manifest (Mac)
python3 /opt/axentx/airship/surrogate/scripts/generate_manifest.py

# 2. Check CDN URLs are reachable (no auth)
curl -I "https://huggingface.co/datasets/axentx/surrogate-dataset-mirror/resolve/main/batches/mirror-merged/2026-05-03/example.parquet"

# 3. Launch training (reuses studio)
python3 /opt/axentx/airship/surrogate/scripts/launch_training.py

# 4. Confirm zero HF API calls during training
# (Check logs: should see only CDN 200s, no 429s)
```

**Expected outcome**:  
- ✅ HF API calls reduced to **1 per day** (manifest generation)  
- ✅ CDN downloads unlimited (bypasses 429)  
- ✅ Lightning quota saved via Studio reuse  
- ✅ Training survives idle-stop via status check + restart
