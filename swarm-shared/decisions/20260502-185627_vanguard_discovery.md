# vanguard / discovery

## Final synthesized answer

**Goal:** One reliable, reusable discovery-and-launch workflow that surfaces high-value knowledge before planning, avoids HF rate limits during training ingestion, and prevents Lightning quota waste by reusing studios.

---

### 1) What to create
- `/opt/axentx/vanguard/discover_and_launch.sh` — main orchestration script (executable, proper shebang, `set -euo pipefail`, `IFS=$'\n\t'`).
- `/opt/axentx/vanguard/discovery/` directory with a short `README.md` explaining the discovery workflow and artifact conventions.
- Optional convenience helper `/opt/axentx/vanguard/bin/discover-top-hub.sh` (thin wrapper that calls the main script’s discovery phase) for interactive use.

---

### 2) Core behavior (combined + resolved)

1. **Business research**  
   - If `granite-business-research.sh` exists, run it (with retries).  
   - Skip cleanly if already run today (use a sentinel file in `discovery/` to avoid redundant runs).  
   - Always tag and link outputs (e.g., `discovery/business-research-{date}.json`) for traceability.

2. **Knowledge-RAG top-hub query**  
   - Query the top-connected hub (MOC or highest-degree node) via `knowledge-rag`.  
   - Emit a compact JSON report to `discovery/top-hub-{date}.json` with fields: `hub`, `insights`, `tags`, `links`, `timestamp`.  
   - Print concise, actionable insights to stdout for immediate planning use.  
   - If `knowledge-rag` is unavailable, log a clear skip message and exit code 0 (do not fail the whole run).

3. **HF dataset file list (CDN-only, rate-limit-safe)**  
   - Use a single non-recursive `list_repo_tree` call for `{HF_REPO}/{DATE_FOLDER}`.  
   - Write `file_list.json` containing: `repo`, `folder`, `files[]`, `cdn_prefix`, `generated_at`.  
   - Keep listing minimal (one API call) and CDN-ready: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.  
   - If listing fails or is empty, log a warning and exit non-zero only when `--strict` is passed (default: continue so discovery still works).

4. **Lightning Studio reuse (quota-safe)**  
   - Prefer reuse: if a studio with the expected name is running, use it.  
   - If stopped, restart it on the specified machine (L40S).  
   - Only create if it does not exist.  
   - Use Lightning SDK with short timeouts and clear logging; never blindly create duplicates.  
   - Make machine configurable via env (default `L40S`).

5. **Orchestration hygiene**  
   - Shebang: `#!/usr/bin/env bash`.  
   - Use `retry` helper (configurable `MAX_RETRIES`, `RETRY_WAIT`).  
   - All steps log with UTC timestamps.  
   - Provide `--no-training`, `--strict`, `--hf-repo`, `--date-folder` flags.  
   - Crontab guidance with `SHELL=/bin/bash` and log rotation recommendation.

---

### 3) Recommended implementation

```bash
#!/usr/bin/env bash
# /opt/axentx/vanguard/discover_and_launch.sh
# Orchestrates discovery + safe launch for vanguard.
# Usage: discover_and_launch.sh [--no-training] [--strict] [--hf-repo <repo>] [--date-folder <path>]

set -euo pipefail
IFS=$'\n\t'

# ---- Config ----
HF_REPO="${HF_REPO:-datasets/axentx/vanguard-mirror}"
DATE_FOLDER="${DATE_FOLDER:-$(date +%Y-%m-%d)}"
FILE_LIST="file_list.json"
LIGHTNING_TEAMSPACE="${LIGHTNING_TEAMSPACE:-default}"
STUDIO_NAME="vanguard-l40s"
MACHINE="${MACHINE:-L40S}"
MAX_RETRIES="${MAX_RETRIES:-3}"
RETRY_WAIT="${RETRY_WAIT:-60}"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISCOVERY_DIR="${BASE_DIR}/discovery"

mkdir -p "$DISCOVERY_DIR"

# ---- Args ----
NO_TRAINING=0
STRICT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-training) NO_TRAINING=1; shift ;;
    --strict) STRICT=1; shift ;;
    --hf-repo) HF_REPO="$2"; shift 2 ;;
    --date-folder) DATE_FOLDER="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ---- Helpers ----
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

retry() {
  local n=0
  until "$@"; do
    n=$((n+1))
    if [ "$n" -ge "$MAX_RETRIES" ]; then
      log "ERROR: Command failed after $MAX_RETRIES attempts: $*"
      return 1
    fi
    log "WARN: Command failed (attempt $n/$MAX_RETRIES). Retrying in ${RETRY_WAIT}s..."
    sleep "$RETRY_WAIT"
  done
}

sentinel() {
  local name="$1"
  local file="${DISCOVERY_DIR}/.sentinel-${name}-$(date +%Y-%m-%d)"
  if [[ -f "$file" ]]; then
    return 0
  fi
  touch "$file"
  return 1
}

# ---- 1) Business research ----
if command -v granite-business-research.sh >/dev/null 2>&1; then
  if sentinel "business-research"; then
    log "SKIP: Business research already run today (sentinel present)."
  else
    log "Running business research..."
    if retry granite-business-research.sh; then
      log "Business research completed."
      # Tag and link artifact
      cp -f "$(find . -maxdepth 2 -name 'business-research-*.json' -type f | head -1)" \
        "${DISCOVERY_DIR}/business-research-${DATE_FOLDER}.json" 2>/dev/null || true
    else
      log "WARN: Business research failed; continuing."
    fi
  fi
else
  log "SKIP: granite-business-research.sh not found (stub mode)."
fi

# ---- 2) Knowledge-RAG top-hub ----
TOP_HUB_REPORT="${DISCOVERY_DIR}/top-hub-${DATE_FOLDER}.json"
if command -v knowledge-rag >/dev/null 2>&1; then
  log "Querying knowledge-rag for top hub (MOC/highest-degree)..."
  if retry knowledge-rag --query "top hub MOC" --format concise > "${DISCOVERY_DIR}/top-hub-insights.txt" 2>&1; then
    hub_name="MOC"
    insights="$(cat "${DISCOVERY_DIR}/top-hub-insights.txt")"
  else
    log "WARN: knowledge-rag query failed; using fallback."
    hub_name="unknown"
    insights="knowledge-rag unavailable or query failed"
  fi
else
  log "SKIP: knowledge-rag not installed. Install to enable hub insights. Tags: #knowledge-rag #graph #hub"
  hub_name="unavailable"
  insights="knowledge-rag not installed"
fi

cat > "$TOP_HUB_REPORT" <<EOF
{
  "hub": "$hub_name",
  "insights": "$(echo "$insights" | sed 's/"/\\"/g' | tr '\n' ' ')",
  "tags": ["business-research","knowledge-rag","graph","hub"],
  "links": {
    "business_research": "${DISCOVERY_DIR}/business-research-${DATE_FOLDER}.json",
    "top_hub_report": "$TOP_HUB_REPORT"
  },
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
log "Top-hub report saved to $TOP_HUB_REPORT"
cat "${DISCOVERY_DIR}/top-hub-insights.txt" 2>/dev/null || true

# ---- 3) HF file list (CDN-only) ----
log "Listing HF dataset files (single API call) for ${HF_REPO}/${DATE_FOLDER}..."
python3 - "$HF_REPO" "$DATE_FOLDER" "$FILE_LIST" <<'PY'
import json, os,
