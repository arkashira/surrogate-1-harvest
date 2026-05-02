# surrogate-1 / discovery

## Implementation plan (≤2h)

1. Add `YYYY-MM-DD` date partition to output path (deterministic, prevents overwrite races).
2. Replace recursive `list_repo_files` with per-folder `list_repo_tree(recursive=False)` and cache results.
3. Emit a `file-list.json` (date-folder → CDN URLs) during pre-flight on the Mac orchestrator; embed it in the worker so training uses CDN-only fetches (zero API calls during data load).
4. Keep HF write for final upload, but use CDN URLs for all dataset streaming/reads.
5. Make script robust: shebang, executable, `SHELL=/bin/bash` in cron, reuse running Lightning Studio, handle idle-stop.

---

### 1. Update `bin/dataset-enrich.sh` (deterministic date + CDN URLs)

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Deterministic date-partitioned shard ingest with CDN-bypass.
# Usage:
#   HF_TOKEN=... SHARD_ID=0 SHARD_COUNT=16 ./bin/dataset-enrich.sh 2025-11-01

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date -u +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
SHARD_COUNT="${SHARD_COUNT:-16}"
TS=$(date -u +%H%M%S)
OUTFILE="batches/public-merged/${DATE}/shard${SHARD_ID}-${TS}.jsonl"
FILE_LIST="${DATE}-file-list.json"

# ---- pre-flight: produce file-list for this date folder (once per run) ----
# If file-list exists and is non-empty, reuse it (idempotent).
if [[ ! -s "${FILE_LIST}" ]]; then
  echo "[$(date -u)] Generating file-list for ${DATE} ..."
  python3 - "$REPO" "$DATE" "$FILE_LIST" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
date_folder = sys.argv[2]
out_path = sys.argv[3]

api = HfApi()
# Non-recursive per folder to avoid huge pagination.
tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
entries = []
for item in tree:
    if item.type == "file":
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{item.path}"
        entries.append({"path": item.path, "cdn_url": cdn_url})
with open(out_path, "w") as f:
    json.dump(entries, f)
PY
fi

# ---- stream & normalize shard slice ----
python3 - "$SHARD_ID" "$SHARD_COUNT" "$FILE_LIST" "$OUTFILE" <<'PY'
import json, hashlib, sys, os, requests
from datasets import load_dataset

shard_id = int(sys.argv[1])
shard_count = int(sys.argv[2])
file_list_path = sys.argv[3]
outfile = sys.argv[4]

with open(file_list_path) as f:
    files = json.load(f)

# Deterministic shard assignment by filename hash.
def shard_for(path):
    return int(hashlib.md5(path.encode()).hexdigest(), 16) % shard_count

my_files = [f for f in files if shard_for(f["path"]) == shard_id]

os.makedirs(os.path.dirname(outfile), exist_ok=True)

with open(outfile, "w") as out:
    for entry in my_files:
        cdn_url = entry["cdn_url"]
        # Use CDN URL directly; load_dataset supports http(s) paths.
        # Project to {prompt,response} at parse time (schema normalization).
        try:
            ds = load_dataset("json", data_files=cdn_url, split="train", streaming=True)
            for row in ds:
                # Minimal projection + deterministic hash for dedup downstream.
                prompt = row.get("prompt") or row.get("input") or ""
                response = row.get("response") or row.get("output") or ""
                if not prompt or not response:
                    continue
                payload = {"prompt": prompt, "response": response}
                payload_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
                payload_hash = hashlib.md5(payload_str.encode()).hexdigest()
                payload["_id"] = payload_hash
                out.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
        except Exception as e:
            # Log and continue; don't fail entire shard.
            print(f"[WARN] {cdn_url}: {e}", file=sys.stderr)
PY

# ---- upload to HF (single commit per shard+timestamp) ----
huggingface-cli upload --repo-type dataset "$REPO" "$OUTFILE" "$OUTFILE"
echo "[$(date -u)] Shard ${SHARD_ID} uploaded: ${OUTFILE}"
```

Make executable:

```bash
chmod +x bin/dataset-enrich.sh
```

---

### 2. GitHub Actions matrix (unchanged behavior, passes date)

`.github/workflows/ingest.yml` (only relevant excerpt):

```yaml
jobs:
  ingest:
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: |
          chmod +x bin/dataset-enrich.sh
          HF_TOKEN=${{ secrets.HF_TOKEN }} \
          SHARD_ID=${{ matrix.shard }} \
          SHARD_COUNT=16 \
          ./bin/dataset-enrich.sh $(date -u +%Y-%m-%d)
```

---

### 3. Lightning training script (CDN-only, pre-flight file-list)

`train.py` (orchestrator on Mac → Lightning Studio):

```python
#!/usr/bin/env python3
# train.py
# Lightning training with CDN-only fetches.
# Usage: python train.py 2025-11-01

import json, os, sys, subprocess
from pathlib import Path
from lightning import Fabric, LightningFlow, LightningWork, LightningApp, Studio
from lightning.fabric.plugins import BitsandbytesPrecision
import torch
from datasets import load_dataset

DATE = sys.argv[1] if len(sys.argv) > 1 else "2025-11-01"
FILE_LIST = f"{DATE}-file-list.json"
REPO = "axentx/surrogate-1-training-pairs"

# ---- pre-flight: reuse existing running studio ----
studio = None
for s in Studio.list():
    if s.name == "surrogate-1-train" and s.status == "Running":
        studio = s
        break
if studio is None:
    studio = Studio.create(
        name="surrogate-1-train",
        machine="L40S",
        cloud="lightning-public-prod",
        create_ok=True,
    )

# ---- generate file-list once (Mac orchestrator) ----
if not Path(FILE_LIST).exists():
    from huggingface_hub import HfApi
    api = HfApi()
    tree = api.list_repo_tree(repo=REPO, path=DATE, recursive=False)
    entries = [
        {"path": i.path, "cdn_url": f"https://huggingface.co/datasets/{REPO}/resolve/main/{DATE}/{i.path}"}
        for i in tree if i.type == "file"
    ]
    with open(FILE_LIST, "w") as f:
        json.dump(entries, f)

with open(FILE_LIST) as f:
    cdn_urls = [e["cdn_url"] for e in json.load(f)]

# ---- Lightning training (CDN-only) ----
fabric = Fabric(devices=1, accelerator="cuda", precision="bf16-mixed")

def collate(batch):
    # Expect {prompt, response, _id}
    return batch

# Streaming from CDN URLs (zero HF API calls during training).
train_data = load_dataset("json", data_files=cdn_urls, split="train", streaming=True)
train_loader = torch.utils.data.DataLoader(train_data, batch_size=8, collate_fn=
