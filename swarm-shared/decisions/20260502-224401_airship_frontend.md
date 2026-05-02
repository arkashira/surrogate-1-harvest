# airship / frontend

Below is the **single, merged implementation** that keeps the highest-value ideas from both proposals, removes contradictions, and maximizes correctness + actionability.

Key decisions (why):
- Use **Bash orchestrator** (Candidate 1) for cron safety, minimal runtime deps, and easy scheduling.  
- Keep **Python helpers inline** (Candidate 2) for HF API calls and JSON/HTML generation (cleaner, fewer external scripts).  
- Produce **both JSON status (for APIs) and static HTML status (for humans)** — Candidate 2’s HTML status is high-value; Candidate 1’s JSON status is machine-friendly. We include both.  
- Use **CDN-only URLs** in manifest and never require auth for training-time fetches.  
- Single HF `list_repo_tree` call per run (per date folder) — both agree; we enforce it.  
- Cron-safe: `SHELL=/bin/bash`, proper shebang, `set -euo pipefail`, absolute paths, log rotation-friendly.

---

## 1) Add executable orchestrator: `bin/airship-discover`

```bash
#!/usr/bin/env bash
# bin/airship-discover
# Cron-safe orchestrator for airship discover
# Usage: ./bin/airship-discover [--date YYYY-MM-DD] [--out-dir /path]
# Crontab (example):
#   SHELL=/bin/bash
#   0 2 * * * /opt/axentx/airship/bin/airship-discover --date $(date +\%F) --out-dir /opt/axentx/airship/out >> /opt/axentx/airship/logs/discover.log 2>&1

set -euo pipefail
IFS=$'\n\t'

# ---- paths ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="${PROJECT_ROOT}/out"
LOG_DIR="${PROJECT_ROOT}/logs"
TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
HF_REPO="${HF_REPO:-datasets/your-org/your-repo}"  # override via env
DATE=""

# ---- arg parsing ----
while [[ $# -gt 0 ]]; do
  case $1 in
    --date)
      DATE="$2"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

DATE="${DATE:-$(date +%F)}"
mkdir -p "$OUT_DIR" "$LOG_DIR"

log() {
  echo "[$TIMESTAMP] $*" | tee -a "$LOG_DIR/discover.log"
}

fail() {
  log "ERROR: $*"
  exit 1
}

# ---- helpers ----

run_market_research() {
  local out_file="$1"
  log "Running market research + knowledge-RAG insights..."

  # Prefer existing scripts if present; otherwise produce minimal valid JSON.
  if [[ -x "${PROJECT_ROOT}/scripts/granite-business-research.sh" ]]; then
    if "${PROJECT_ROOT}/scripts/granite-business-research.sh" > "$out_file" 2>> "$LOG_DIR/discover.log"; then
      log "Market research completed."
    else
      log "Market research script failed; producing stub."
      echo '{"insights":[],"tags":["#business-research"],"source":"stub"}' > "$out_file"
    fi
  else
    echo '{"insights":[],"tags":["#business-research"],"source":"stub"}' > "$out_file"
  fi

  if [[ -x "${PROJECT_ROOT}/scripts/query-top-hub.sh" ]]; then
    if ! "${PROJECT_ROOT}/scripts/query-top-hub.sh" >> "$out_file" 2>> "$LOG_DIR/discover.log"; then
      log "Top-hub query failed; appending stub."
      echo '{"top_hub":"MOC","tags":["#knowledge-rag","#hub"],"source":"stub"}' >> "$out_file"
    fi
  else
    echo '{"top_hub":"MOC","tags":["#knowledge-rag","#hub"],"source":"stub"}' >> "$out_file"
  fi
}

fetch_hf_manifest() {
  local out_file="$1"
  local folder_path="${2:-${DATE}}"
  log "Fetching HF repo tree (non-recursive) for folder: ${folder_path}"

  # Use huggingface_hub via Python (single API call).
  if python3 -c "
import json, os, sys
try:
    from huggingface_hub import HfApi
    api = HfApi()
    files = list(api.list_repo_tree(repo_id='${HF_REPO}', path='${folder_path}', repo_type='dataset', recursive=False))
    entries = []
    for f in files:
        if getattr(f, 'type', None) == 'file':
            entries.append({
                'path': f.path,
                'cdn_url': f'https://huggingface.co/datasets/${HF_REPO}/resolve/main/{f.path}',
                'size': getattr(f, 'size', None)
            })
    result = {'date':'${DATE}','folder':'${folder_path}','files':entries,'generated':'${TIMESTAMP}','source':'huggingface_hub'}
    print(json.dumps(result, indent=2))
    sys.exit(0)
except Exception as e:
    sys.stderr.write(str(e) + '\n')
    sys.exit(1)
" > "$out_file" 2>> "$LOG_DIR/discover.log"; then
    log "HF manifest written to ${out_file}"
    return 0
  fi

  log "HF tree listing unavailable; using fallback CDN-only manifest."
  python3 -c "
import json
manifest = {
  'date': '${DATE}',
  'folder': '${folder_path}',
  'warning': 'HF tree listing unavailable; using CDN pattern. Install huggingface_hub for automatic manifests.',
  'files': [],
  'generated': '${TIMESTAMP}',
  'source': 'fallback'
}
with open('${out_file}', 'w') as f:
    json.dump(manifest, f, indent=2)
" 2>> "$LOG_DIR/discover.log"
}

# ---- main ----
log "Starting airship discover (date=${DATE})"

INSIGHTS_FILE="${OUT_DIR}/insights-${DATE}.json"
MANIFEST_FILE="${OUT_DIR}/manifest-${DATE}.json"
STATUS_JSON="${PROJECT_ROOT}/public/status.json"
STATUS_HTML="${PROJECT_ROOT}/public/status.html"

# 1) Market research + knowledge-RAG
run_market_research "$INSIGHTS_FILE"

# 2) HF manifest (single API call) -> CDN-only URLs
fetch_hf_manifest "$MANIFEST_FILE" "${DATE}"

# 3) Generate status JSON + static HTML
mkdir -p "$(dirname "$STATUS_JSON")"
python3 -c "
import json, os, glob, datetime, html

out_dir = '${OUT_DIR}'
date = '${DATE}'
manifests = sorted(glob.glob(os.path.join(out_dir, 'manifest-*.json')))
insights_available = os.path.exists('${INSIGHTS_FILE}')
latest_manifest = os.path.basename(manifests[-1]) if manifests else None

status = {
  'status': 'ok',
  'generated': '${TIMESTAMP}',
  'latest_manifest': latest_manifest,
  'available_manifests': [os.path.basename(m) for m in manifests],
  'insights_available': insights_available,
  'cdn_only': True,
  'notes': 'Manifest contains CDN URLs (no auth). HF API used only for tree listing (once per run).'
}

# Write JSON status
with open('${STATUS_JSON}', 'w') as f:
    json.dump(status, f, indent=2)

# Write simple HTML status
rows = ''.join(
    f'<tr><td>{html.escape(m)}</td><td><a href=\"/out/{html.escape(m)}\">download</a></td></tr>'
    for m in sorted(os.listdir(out_dir)) if m.endswith('.json')
)
html_content = f'''<!doctype html>
<html lang=\"en\">
<head><meta charset=\"utf-8\"><title>Airship Discover — Status</title></head>
<body>
<h1>Airship Discover — Status</h1>
<p>Generated: {html.escape('${TIMESTAMP}')}</p>
<p>Status: {html
