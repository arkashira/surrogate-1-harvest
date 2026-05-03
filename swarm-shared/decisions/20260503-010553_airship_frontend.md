# airship / frontend

## Incremental Improvement Plan (≤2h)

**Title**: Airship Frontend — HF CDN Bypass + Training Manifest + Studio Reuse

**Goal**: Eliminate HF API 429s and `pyarrow.CastError` during surrogate training, reduce Lightning Studio quota burn, and make frontend-driven training launches robust.

**Why this ships highest value in <2h**:
- Removes the most common failure modes (rate-limit, schema mismatch) that block autonomous 24/7 training.
- Enables frontend to launch/correctly-reuse studios without recreating (saves quota).
- Uses CDN-only fetches during training (zero API calls), so training jobs no longer stall on HF auth limits.
- Small, focused changes: one manifest generator, one train.py patch, one frontend studio-reuse helper.

---

## Implementation Plan

### 1) Add training manifest generator (mac orchestration)
- Single API call (after rate-limit window) to list one date folder in a surrogate repo.
- Save `training_manifest_{date}.json` listing only parquet files to be used.
- Embed this manifest in `train.py`; Lightning workers fetch via CDN URLs only.

### 2) Patch surrogate training loader
- Stop using `load_dataset(streaming=True)` on heterogeneous repos.
- Use manifest + `hf_hub_download` (or raw CDN URLs) to fetch listed parquet files.
- Project to `{prompt, response}` at parse time; ignore extra/mixed columns.
- Move attribution to filename pattern: `batches/mirror-merged/{date}/{slug}.parquet`.

### 3) Frontend studio reuse helper
- Before `Studio(create_ok=True)`, list `Teamspace.studios` and reuse any Running studio with matching name.
- If stopped, restart with `target.start(machine=Machine.L40S)` (avoid idle-stop killing training).
- Reduces Lightning quota burn (~80hr/mo saved).

### 4) Add CDN bypass downloader utility
- Public parquet files fetched via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no Authorization header).
- CDN tier has much higher limits; avoids API 429 entirely during data loading.

---

## Code Snippets

### 1) Manifest generator (mac orchestration script)
```bash
#!/usr/bin/env bash
# generate_training_manifest.sh
# Run from mac after rate-limit window clears.
# Produces training_manifest_YYYYMMDD.json for Lightning training.

set -euo pipefail
REPO="axentx/surrogate-mirror"
DATE="${1:-$(date +%Y%m%d)}"
OUT="training_manifest_${DATE}.json"

echo "Listing ${REPO}/batches/mirror-merged/${DATE}/ (non-recursive)..."
# Requires HF_TOKEN in env for API; list once, then CDN-only during training.
python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi
api = HfApi()
files = api.list_repo_tree(
    repo_id="${REPO}",
    path="batches/mirror-merged/${DATE}",
    recursive=False
)
parquets = [f.rfilename for f in files if f.rfilename.endswith(".parquet")]
manifest = {
    "repo": "${REPO}",
    "date": "${DATE}",
    "files": sorted(parquets)
}
with open("${OUT}", "w") as f:
    json.dump(manifest, f, indent=2)
print(f"Wrote ${OUT} with {len(parquets)} files")
PY
```

### 2) CDN-based parquet loader (train.py snippet)
```python
# train.py — surrogate training loader
import json
import pyarrow.parquet as pq
import requests
from io import BytesIO
from pathlib import Path

def load_manifest(manifest_path: str):
    with open(manifest_path) as f:
        return json.load(f)

def cdn_parquet_reader(repo: str, file_path: str):
    # CDN bypass: no Authorization header
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return pq.read_table(BytesIO(resp.content))

def build_dataset(manifest_path: str):
    manifest = load_manifest(manifest_path)
    repo = manifest["repo"]
    rows = []
    for f in manifest["files"]:
        table = cdn_parquet_reader(repo, f)
        # Project only expected columns; ignore mixed schema extras
        if "prompt" not in table.column_names or "response" not in table.column_names:
            continue
        prompts = table["prompt"].to_pylist()
        responses = table["response"].to_pylist()
        for p, r in zip(prompts, responses):
            if p is not None and r is not None:
                rows.append({"prompt": p, "response": r})
    return rows
```

### 3) Frontend studio reuse helper (surrogate SDK wrapper)
```python
# surrogate/studio_launcher.py
from lightning import Studio, Teamspace, Machine

def get_or_create_studio(name: str, machine: Machine = Machine.L40S):
    # Reuse running studio to save quota
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    # If exists but stopped, restart
    for s in Teamspace.studios:
        if s.name == name and s.status == "Stopped":
            print(f"Restarting stopped studio: {name}")
            s.start(machine=machine)
            return s
    # Create new only if none exist
    print(f"Creating studio: {name}")
    return Studio(name=name, create_ok=True, machine=machine)
```

### 4) Lightweight orchestration wrapper (for cron/airflow)
```bash
#!/usr/bin/env bash
# launch_surrogate_training.sh
# Ensures proper env and studio reuse before training run.

set -euo pipefail
export SHELL=/bin/bash

MANIFEST="training_manifest_$(date +%Y%m%d).json"
if [[ ! -f "$MANIFEST" ]]; then
  echo "Manifest missing: $MANIFEST — generating..."
  ./generate_training_manifest.sh "$(date +%Y%m%d)"
fi

python3 -m surrogate.studio_launcher &
STUDIO_PID=$!

python3 -m surrogate.train \
  --manifest "$MANIFEST" \
  --epochs 3 \
  --output-dir "outputs/$(date +%Y%m%d)"

wait $STUDIO_PID || true
```

---

## Acceptance Criteria
- [ ] Manifest generator produces valid JSON listing parquet files for a date folder.
- [ ] Training script loads only listed parquet files via CDN (no HF API calls during data loading).
- [ ] Mixed-schema files no longer cause `pyarrow.CastError`; only `prompt`/`response` used.
- [ ] Frontend/launcher reuses running studios and restarts stopped ones instead of recreating.
- [ ] No HF API 429 observed during training data loading (verify logs for CDN-only fetches).

## Rollout
1. Place `generate_training_manifest.sh` in repo root and make executable.
2. Replace surrogate training loader with CDN-based loader snippet.
3. Add `studio_launcher.py` and update launch scripts to use `get_or_create_studio`.
4. Update cron/airflow to run manifest generation once per day, then launch training.
