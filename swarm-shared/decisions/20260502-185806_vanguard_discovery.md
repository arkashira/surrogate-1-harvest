# vanguard / discovery

## 1) Diagnosis

- No executable discovery entrypoint exists to surface high-value knowledge (top-hub docs, contextual insights) before planning work.
- Missing CDN-bypass file-list strategy for HF datasets; any future training ingestion will immediately hit 429 rate limits without a pre-cached file manifest.
- No guard against recreating Lightning Studios; iterating training scripts risks burning 80+ hrs/month quota by spawning duplicates instead of reusing running instances.
- No clear pattern for invoking business-research + knowledge-rag in a single flow to produce actionable context for the current task.
- Missing lightweight validation that the repo is in a runnable state (executable wrappers, shebangs, PATH) to avoid past script-error patterns.

## 2) Proposed change

Create `/opt/axentx/vanguard/discovery/run_discovery.sh` as the single entrypoint that:
- Runs business-research + knowledge-rag query for top-hub insights (MOC).
- Pre-lists one date folder of a target HF dataset via single API call and writes `file_list.json` for CDN-bypass training.
- Checks for a running Lightning Studio and reuses it (or reports ready-to-start).
- Validates critical wrapper scripts have shebang + executable bit.

## 3) Implementation

```bash
#!/usr/bin/env bash
# /opt/axentx/vanguard/discovery/run_discovery.sh
# Purpose: discovery entrypoint — surface knowledge, prepare HF file list, reuse Lightning Studio
set -euo pipefail

# ---- config ----
HF_REPO="${HF_REPO:-datasets/axentx/surrogate-1}"
HF_DATE="${HF_DATE:-$(date +%Y-%m-%d)}"
OUT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FILE_LIST="${OUT_DIR}/file_list.json"
LIGHTNING_NAME="${LIGHTNING_NAME:-vanguard-train-l40s}"

# ---- helpers ----
log() { echo "[$(date +%H:%M:%S)] $*"; }

# ---- 1) knowledge-rag top-hub insight ----
log "Running business-research + knowledge-rag for top-hub insight..."
if command -v knowledge-rag >/dev/null 2>&1; then
  knowledge-rag --query "top hub and related docs for planning (MOC)" --format concise --out "${OUT_DIR}/top_hub_insight.md"
  log "Top-hub insight saved to ${OUT_DIR}/top_hub_insight.md"
else
  log "knowledge-rag not installed; skipping RAG step (install to enable)."
fi

# ---- 2) HF CDN-bypass: pre-list one date folder ----
log "Pre-listing HF folder for CDN-bypass (single API call)..."
python3 - "$HF_REPO" "$HF_DATE" "$FILE_LIST" <<'PY'
import os, json, sys
from huggingface_hub import list_repo_tree

repo, date_folder, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
# non-recursive single-level list to minimize API usage
items = list_repo_tree(repo=repo, path=date_folder, recursive=False)
files = [it.path for it in items if it.type == "file"]
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w") as f:
    json.dump({"repo": repo, "date": date_folder, "files": files}, f, indent=2)
print(f"Listed {len(files)} files -> {out_path}")
PY
log "File list saved to ${FILE_LIST}"

# ---- 3) Lightning Studio reuse guard ----
log "Checking Lightning Studio reuse..."
python3 - "$LIGHTNING_NAME" <<'PY'
import sys, os
from lightning_sdk import Teamspace, Studio, Machine

name = sys.argv[1]
team = Teamspace.current()
running = None
for s in team.studios:
    if s.name == name and s.status == "running":
        running = s
        break

if running:
    print(f"Reuse existing running studio: {running.name} (id={running.id})")
else:
    print(f"No running studio named '{name}'. To start one:")
    print(f"  python3 -c \"from lightning_sdk import Teamspace, Studio, Machine; Teamspace.current().studios.create(name='{name}', machine=Machine.L40S, create_ok=True)\"")
PY

# ---- 4) wrapper script hygiene check ----
log "Checking wrapper scripts (shebang + executable)..."
find "${OUT_DIR}" -maxdepth 2 -type f -name "*.sh" -o -name "*.bash" | while read -r f; do
  if ! head -1 "$f" | grep -qE '^#!/usr/bin/env (bash|sh)'; then
    log "WARN: missing Bash shebang in $f"
  fi
  if [[ ! -x "$f" ]]; then
    log "WARN: not executable: $f (run: chmod +x '$f')"
  fi
done
log "Discovery complete."
```

Make it executable:
```bash
chmod +x /opt/axentx/vanguard/discovery/run_discovery.sh
```

## 4) Verification

1. Run the script:
   ```bash
   cd /opt/axentx/vanguard/discovery && ./run_discovery.sh
   ```
2. Confirm outputs:
   - `top_hub_insight.md` exists and contains a short summary (or a note that knowledge-rag is unavailable).
   - `file_list.json` exists and lists files for today’s HF folder (non-empty array).
   - Studio check prints either a running studio ID or a clear command to start one.
   - No errors about missing shebangs/executable bits for local `.sh` files (or warnings are expected and actionable).
3. Confirm zero API calls during a simulated training run by checking that `file_list.json` is consumed by a downstream `train.py` that fetches only via CDN URLs (implementation for `train.py` can follow in a separate 1–2h task).
