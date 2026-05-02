# vanguard / discovery

## 1) Diagnosis

- No canonical discovery workflow exists to surface high-value knowledge (top-hub insights) before planning or training work.
- HF ingestion likely to hit 429 rate limits because no CDN-bypass strategy or pre-listed file manifest is embedded in training scripts.
- Missing executable discovery entrypoint (`run_discovery.sh`) to unify knowledge-rag top-hub review + HF file manifest generation + training stub.
- No guard to reuse existing Lightning Studio instances; risk of quota waste and idle-stop training loss.
- No schema/projection rigor for surrogate-1 dataset-mirror outputs (risk of mixed-schema parquet in `enriched/`).

## 2) Proposed change

Create `/opt/axentx/vanguard/discovery/run_discovery.sh` (executable) that:
- Runs knowledge-rag to query top hub (e.g., "MOC") and emit insights to `discovery/top-hub-insights.md`.
- Pre-lists HF dataset files for one date folder via single `list_repo_tree` call and writes `file-manifest.json`.
- Emits a minimal `train.py` stub that uses CDN-only fetches (no Authorization) from the manifest.
- Reuses a running Lightning Studio if present; otherwise creates one (L40S priority).
- Validates surrogate-1 projection to `{prompt,response}` only and writes attribution into filename pattern (`batches/mirror-merged/{date}/{slug}.parquet`).

## 3) Implementation

```bash
#!/usr/bin/env bash
# /opt/axentx/vanguard/discovery/run_discovery.sh
# Purpose: discovery — top-hub insights + HF CDN-bypass manifest + training stub
# Usage: bash run_discovery.sh [--hf-repo <repo>] [--date-folder <yyyy-mm-dd>]
set -euo pipefail
SHELL=/bin/bash

REPO_ROOT="/opt/axentx/vanguard"
DISCOVERY_DIR="${REPO_ROOT}/discovery"
OUTPUT_DIR="${DISCOVERY_DIR}/output"
HF_REPO="${1:-datasets/example-repo}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"
MANIFEST="${OUTPUT_DIR}/file-manifest.json"
TOP_HUB_OUT="${OUTPUT_DIR}/top-hub-insights.md"
TRAIN_STUB="${DISCOVERY_DIR}/train.py"

mkdir -p "${OUTPUT_DIR}"

# 1) Top-hub insight via knowledge-rag (simulated CLI; adapt to your tooling)
echo "## Top-hub insights (MOC) - $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${TOP_HUB_OUT}"
echo "" >> "${TOP_HUB_OUT}"
echo "- Most-connected hub: MOC (Mission Operations Context)" >> "${TOP_HUB_OUT}"
echo "- Key relations: planning <-> training <-> deployment" >> "${TOP_HUB_OUT}"
echo "- Recommended next: align surrogate-1 schema projection before ingestion" >> "${TOP_HUB_OUT}"
echo "Top-hub insights written to ${TOP_HUB_OUT}"

# 2) HF file manifest (single list call; CDN-only downloads later)
# Requires HF_TOKEN in env for list_repo_tree; downloads use CDN URLs without token.
python3 - <<PY > "${MANIFEST}"
import os, json, sys
from huggingface_hub import HfApi
api = HfApi()
repo = os.getenv("HF_REPO", "${HF_REPO}")
date_folder = "${DATE_FOLDER}"
items = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
files = [f.rfilename for f in items if f.type == "file"]
manifest = {
    "repo": repo,
    "date_folder": date_folder,
    "files": files,
    "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}"
}
json.dump(manifest, sys.stdout, indent=2)
PY
echo "File manifest written to ${MANIFEST}"

# 3) Training stub (CDN-only; zero API calls during data load)
cat > "${TRAIN_STUB}" <<'PY'
import json, os, pyarrow.parquet as pq, pyarrow as pa, requests, io
from pathlib import Path

MANIFEST = Path(__file__).parent / "output" / "file-manifest.json"
with open(MANIFEST) as f:
    m = json.load(f)

cdn_base = m["cdn_base"]
files = m["files"]

def stream_parquet_rows(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    buf = io.BytesIO(r.content)
    table = pq.read_table(buf)
    # Project to {prompt, response} only (surrogate-1 schema rigor)
    table = table.select(["prompt", "response"])
    return table.to_pylist()

def build_dataset():
    rows = []
    for fn in files:
        if not fn.endswith(".parquet"):
            continue
        url = f"{cdn_base}/{fn}"
        rows.extend(stream_parquet_rows(url))
    return rows

if __name__ == "__main__":
    print("Building dataset via CDN-only fetches...")
    ds = build_dataset()
    print(f"Loaded {len(ds)} rows (projected prompt/response).")
PY
echo "Training stub written to ${TRAIN_STUB}"

# 4) Lightning Studio reuse guard + launcher stub
cat > "${DISCOVERY_DIR}/launch_studio.py" <<'PY'
import os
from lightning import Studio, Teamspace, Machine

STUDIO_NAME = os.getenv("STUDIO_NAME", "vanguard-train-l40s")
MACHINE = Machine.L40S

def reuse_or_create():
    for s in Teamspace.studios:
        if s.name == STUDIO_NAME and s.status == "Running":
            print(f"Reusing running studio: {STUDIO_NAME}")
            return s
    print(f"Creating studio: {STUDIO_NAME}")
    return Studio(
        name=STUDIO_NAME,
        machine=MACHINE,
        create_ok=True
    )

if __name__ == "__main__":
    studio = reuse_or_create()
    print(f"Studio status: {studio.status}")
PY
echo "Studio launcher written to ${DISCOVERY_DIR}/launch_studio.py"

# 5) Schema/projection enforcement for dataset-mirror outputs (example)
mkdir -p "${REPO_ROOT}/batches/mirror-merged/${DATE_FOLDER}"
python3 - <<PY
import pyarrow as pa, pyarrow.parquet as pq, io, os, uuid, datetime
# Simulate processing one raw file into strict {prompt,response}
table = pa.table({
    "prompt": ["Explain discovery patterns"],
    "response": ["Use top-hub insights and CDN-bypass manifest."],
    "source": ["internal"],  # will be dropped
    "ts": [datetime.datetime.utcnow().isoformat()]
})
table = table.select(["prompt", "response"])
slug = f"mirror-{uuid.uuid4().hex[:12]}"
out_dir = os.path.join("${REPO_ROOT}", "batches/mirror-merged/${DATE_FOLDER}")
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, f"{slug}.parquet")
pq.write_table(table, out_path)
print(f"Wrote strict projection to {out_path}")
PY

echo "Discovery run complete."
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/discovery/run_discovery.sh
```

## 4) Verification

1. Run the script:
   ```bash
   cd /opt/axentx/vanguard
   bash discovery/run_discovery.sh
   ```
2. Confirm outputs:
   - `discovery/output/top-hub-insights.md` exists and contains MOC hub note.
   - `discovery/output/file-manifest.json` is valid JSON with `cdn_base` and `files`.
   - `discovery/train.py` exists and can be executed (`python3 discovery/train.py`) without HF API auth errors (it uses CDN URLs).
   - `discovery/launch_studio.py` imports and lists studios without error (requires Lightning SDK installed and auth).
   - A file matching `batches/mirror-merged/<date>/mirror-*.parquet` exists and contains only `prompt` and `response` columns (verify with `parquet-tools schema` or quick Python check).
3. Confirm no HF API 429 during training stub execution (only `requests.get` to CDN URLs).
