# vanguard / discovery

## Final synthesized implementation

```bash
#!/usr/bin/env bash
# /opt/axentx/vanguard/discovery/run_discovery.sh
# Purpose: Single-pass discovery — surface top-hub knowledge, prepare HF CDN-bypass file list,
#          reuse running Lightning Studio, project local data, and emit actionable report.
# Usage:  ./run_discovery.sh [HF_REPO] [DATE_FOLDER] [OUT_DIR]
#         defaults: axentx/surrogate-1  batches/mirror-merged/2026-05-02  ./out
set -euo pipefail
SHELL=/bin/bash

# ---- Configuration ----
HF_REPO="${1:-axentx/surrogate-1}"
DATE_FOLDER="${2:-batches/mirror-merged/2026-05-02}"
OUT_DIR="${3:-/opt/axentx/vanguard/discovery/out}"
REPORT="${OUT_DIR}/report.md"
FILE_LIST="${OUT_DIR}/file_list.json"
PROJECTED="${OUT_DIR}/projected.parquet"
STUDIO_INFO="${OUT_DIR}/studio.json"

mkdir -p "${OUT_DIR}"

log() { echo "[$(date -Iseconds)] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

# ---- 0) Lightweight runnable-state verification ----
# Ensure critical wrappers (if present) are executable + have shebang.
for wrapper in /opt/axentx/vanguard/bin/business-research /opt/axentx/vanguard/bin/knowledge-rag; do
  if [[ -f "$wrapper" ]]; then
    [[ -x "$wrapper" ]] || fail "$wrapper exists but is not executable"
    head -1 "$wrapper" | grep -q "^#!" || fail "$wrapper missing shebang"
  fi
done

# ---- 1) Business-research + knowledge-rag contextual pass ----
# If available, run them; otherwise produce a safe placeholder top-hub insight.
TOP_HUB="MOC"
INSIGHT="Most-connected hub is MOC; prioritize tasks that tighten MOC coupling to accelerate discovery feedback loops."

if [[ -x /opt/axentx/vanguard/bin/business-research ]]; then
  log "Running business-research..."
  /opt/axentx/vanguard/bin/business-research --out-dir "${OUT_DIR}" || fail "business-research failed"
fi

if [[ -x /opt/axentx/vanguard/bin/knowledge-rag ]]; then
  log "Running knowledge-rag..."
  /opt/axentx/vanguard/bin/knowledge-rag --query "top-hub insight for ${TOP_HUB}" --out "${OUT_DIR}/rag_insight.json" || fail "knowledge-rag failed"
  # If rag produced a usable insight, prefer it.
  if [[ -f "${OUT_DIR}/rag_insight.json" ]]; then
    TOP_HUB=$(python3 -c "
import json, sys
try:
    with open('${OUT_DIR}/rag_insight.json') as f:
        d=json.load(f)
    print(d.get('hub','${TOP_HUB}'))
except Exception:
    print('${TOP_HUB}')
" 2>/dev/null || echo "${TOP_HUB}")
    INSIGHT=$(python3 -c "
import json, sys
try:
    with open('${OUT_DIR}/rag_insight.json') as f:
        d=json.load(f)
    print(d.get('insight','${INSIGHT}'))
except Exception:
    print('${INSIGHT}')
" 2>/dev/null || echo "${INSIGHT}")
  fi
fi

# ---- 2) HF CDN-bypass: list once, save for training ----
log "Listing HF repo tree (non-recursive) for ${DATE_FOLDER} ..."
python3 - "${HF_REPO}" "${DATE_FOLDER}" "${FILE_LIST}" <<'PY'
import json, os, sys
from huggingface_hub import HfApi
repo_id, path, out = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi()
tree = api.list_repo_tree(repo_id, path=path, recursive=False)
files = [f.rfilename for f in tree if getattr(f, "type", None) == "file"]
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    json.dump(files, f, indent=2)
print(f"Wrote {len(files)} files to {out}")
PY

# ---- 3) Lightning Studio reuse guard ----
log "Checking Lightning Studio..."
python3 - "${STUDIO_INFO}" <<'PY'
import json, sys
out = sys.argv[1]
studio_name = "vanguard-discovery-studio"
reused = False
try:
    from lightning import Teamspace
    for s in Teamspace.studios:
        if getattr(s, "name", None) == studio_name and getattr(s, "status", None) == "running":
            reused = True
            break
except Exception:
    pass
with open(out, "w") as f:
    json.dump({"reused": reused, "name": studio_name}, f)
PY

# ---- 4) Schema projection: keep only {prompt,response} from local parquet/jsonl ----
if compgen -G "/opt/axentx/vanguard/data/**/*.parquet" > /dev/null || compgen -G "/opt/axentx/vanguard/data/**/*.jsonl" > /dev/null; then
  log "Projecting local data to {prompt,response}..."
  python3 - "${PROJECTED}" <<'PY'
import pandas as pd, glob, sys, os
out = sys.argv[1]
frames = []
for p in glob.glob("/opt/axentx/vanguard/data/**/*.parquet", recursive=True):
    try:
        df = pd.read_parquet(p)
        frames.append(df)
    except Exception:
        continue
for p in glob.glob("/opt/axentx/vanguard/data/**/*.jsonl", recursive=True):
    try:
        df = pd.read_json(p, lines=True)
        frames.append(df)
    except Exception:
        continue
if not frames:
    pd.DataFrame(columns=["prompt", "response"]).to_parquet(out, index=False)
else:
    merged = pd.concat(frames, ignore_index=True)
    proj = pd.DataFrame({
        "prompt": merged.get("prompt", merged.get("input", merged.get("question", ""))),
        "response": merged.get("response", merged.get("output", merged.get("answer", "")))
    })
    proj.to_parquet(out, index=False)
print(f"Projected {len(proj)} rows to {out}")
PY
else
  log "No local data found; creating empty projection."
  python3 -c "import pandas as pd; pd.DataFrame(columns=['prompt','response']).to_parquet('${PROJECTED}', index=False)"
fi

# ---- 5) Emit report ----
log "Writing report to ${REPORT}..."
python3 - "${REPORT}" "${FILE_LIST}" "${STUDIO_INFO}" "${TOP_HUB}" "${INSIGHT}" <<'PY'
import json, datetime, sys, os
report_path, file_list_path, studio_info_path, top_hub, insight = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

with open(report_path, "w") as f:
    f.write(f"# Vanguard Discovery Report\n")
    f.write(f"Date: {datetime.datetime.utcnow().isoformat()}Z\n\n")
    f.write(f"## Top-hub insight\n")
    f.write(f"- Hub: **{top_hub}**\n")
    f.write(f"- Insight: {insight}\n\n")

    f.write(f"## HF CDN-bypass file list\n")
    try:
        with open(file_list_path) as g:
            files = json.load(g)
        f.write(f"- Count: {len(files)}\n")
        f.write(f"- Sample: {files[:3]}\n\n")
    except Exception:
        f.write("- File list unavailable.\n\n")

    f.write(f"## Lightning Studio\n")
    try:
        with open(studio_info_path) as g:
            st = json.load(g)
        f.write(f"- Reused: {st.get('reused')}\n")
        f.write(f"- Name: {st.get('name')}\n\n")
    except Exception:
        f.write("- Studio info unavailable.\n\n")

    f.write(f"## Recommended next actions\n")
    f.write(f"1. Attach to running studio or start one (L40S preferred).\n")
    f.write(f"2. Train using CDN-only data load (
