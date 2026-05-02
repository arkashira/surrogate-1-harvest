# airship / frontend

## Final synthesized answer

**Highest-value improvement (≤2h):**  
Ship a **cron-safe, frontend-first `airship discover` orchestrator** that:

1. Runs market research (if present) + knowledge-RAG top-hub query → **tagged insights JSON**.
2. Calls HF `list_repo_tree` **once per date folder** → **manifest** (CDN-only file list) so training can use `resolve/main/...` URLs and bypass HF API rate limits.
3. Emits a **static status page** (`status/discover.html` + `status/discover.json`) for immediate UI observability and cron-safe operation.

This unifies the known patterns, removes HF API pressure during training, and gives the frontend immediate last-run state without backend round-trips.

---

## Implementation plan (≤2h)

| Step | Owner | Time | Details |
|------|-------|------|---------|
| 1 | Frontend | 15m | Add `bin/airship` (Bash) and `src/orchestrators/discover.js` (Node) wrappers; make `bin/airship` the cron entrypoint. |
| 2 | Frontend | 20m | Implement `discover` orchestrator: run market script → run knowledge-RAG query → save tagged insights JSON. |
| 3 | Frontend | 20m | Implement HF manifest: `list_repo_tree` once per date folder → `manifests/{date}/files.json`. |
| 4 | Frontend | 15m | Generate static status: `status/discover.json` + `status/discover.html` (last run, links, timestamps, exit codes). |
| 5 | Frontend | 15m | Add frontend `/status` route handler (serve static JSON) and small status panel component. |
| 6 | Frontend | 15m | Make cron-safe: lockfile (`/var/lock/airship-discover.lock`), idempotent outputs, `SHELL=/bin/bash` hint, proper shebang, `set -euo pipefail`. |
| 7 | Frontend | 20m | Wire and test: local smoke test + dry-run crontab entry. |
| 8 | Frontend | 10m | Buffer/contingency: fallback behavior when scripts or HF API are unavailable. |

Total: ~2h.

---

## Code snippets

### 1) CLI entrypoint (`bin/airship`) — cron-safe orchestrator

```bash
#!/usr/bin/env bash
# bin/airship
# Cron-safe orchestrator for airship frontend workflows.
# Usage: ./bin/airship discover [--date YYYY-MM-DD] [--out-dir DIR]

set -euo pipefail
export SHELL=/bin/bash

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/var}"
HF_REPO="${HF_REPO:-datasets/axentx/surrogate-mirror}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
LOCKFILE="/var/lock/airship-discover.lock"

log() {
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*"
}

acquire_lock() {
  if command -v flock >/dev/null 2>&1; then
    exec 200>"${LOCKFILE}"
    flock -n 200 || { log "ERROR: another instance is running (lock)"; exit 1; }
  else
    if [[ -f "${LOCKFILE}" ]] && kill -0 "$(cat "${LOCKFILE}" 2>/dev/null || echo 0)" 2>/dev/null; then
      log "ERROR: another instance is running (pidfile)"; exit 1
    fi
    echo $$ > "${LOCKFILE}"
    trap 'rm -f "${LOCKFILE}"' EXIT
  fi
}

ensure_out_dir() {
  mkdir -p "${OUT_DIR}"/{insights,manifests,status}
}

run_market_research() {
  local out="$1"
  local script="${ROOT_DIR}/scripts/granite-business-research.sh"
  if [[ -x "${script}" ]]; then
    log "Running market research: ${script}"
    if bash "${script}" > "${out}.raw.json" 2>"${out}.stderr.log"; then
      jq -c --arg date "$DATE" --arg tag business-research \
        '{date: $date, tag: $tag, source: "granite-business-research", data: .}' \
        "${out}.raw.json" > "${out}" || true
    else
      log "WARN: market research failed (see ${out}.stderr.log)"
      echo '{"error": "market research failed"}' > "${out}"
    fi
  else
    log "SKIP: market research script not found (${script})"
    echo '{"skipped": "market research script not found"}' > "${out}"
  fi
}

run_knowledge_rag() {
  local out="$1"
  local query="${2:-top hub}"
  local script="${ROOT_DIR}/scripts/knowledge-rag.sh"
  if [[ -x "${script}" ]]; then
    log "Running knowledge-RAG query: ${query}"
    if bash "${script}" --query "$query" > "${out}.raw.json" 2>"${out}.stderr.log"; then
      jq -c --arg date "$DATE" --arg tag knowledge-rag --arg query "$query" \
        '{date: $date, tag: $tag, query: $query, data: .}' \
        "${out}.raw.json" > "${out}" || true
    else
      log "WARN: knowledge-RAG failed (see ${out}.stderr.log)"
      echo '{"error": "knowledge-RAG failed"}' > "${out}"
    fi
  else
    log "SKIP: knowledge-RAG script not found (${script})"
    echo '{"skipped": "knowledge-RAG script not found"}' > "${out}"
  fi
}

build_hf_manifest() {
  local out="$1"
  local folder="$2"  # e.g. batches/mirror-merged/2026-05-02
  log "Building HF manifest for ${HF_REPO}/${folder}"
  python3 -c "
import json, sys, os
try:
    from huggingface_hub import list_repo_tree
    tree = list_repo_tree(repo_id='${HF_REPO}', path='${folder}', recursive=False)
    files = sorted([t.path for t in tree if t.type == 'file'])
    manifest = {
        'repo': '${HF_REPO}',
        'folder': '${folder}',
        'date': '${DATE}',
        'files': files,
        'note': 'CDN-only: use resolve/main/... URLs to bypass HF API during training'
    }
    with open('${out}', 'w') as f:
        json.dump(manifest, f, indent=2)
except Exception as e:
    sys.stderr.write(str(e))
    sys.exit(1)
" 2>"${out}.stderr.log" || {
    log "WARN: HF manifest failed (see ${out}.stderr.log)"
    echo '{"error": "HF manifest failed"}' > "${out}"
  }
}

render_status_page() {
  local insights="$1"
  local manifest="$2"
  local status_dir="${OUT_DIR}/status"
  local stamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  local page="${status_dir}/discover.html"
  local json="${status_dir}/discover.json"

  # JSON status
  jq -n \
    --arg date "$DATE" \
    --arg stamp "$stamp" \
    --arg repo "$HF_REPO" \
    --slurpfile insights "${insights}" \
    --slurpfile manifest "${manifest}" \
    '{
      date: $date,
      generated: $stamp,
      repo: $repo,
      insights: $insights[0],
      manifest: $manifest[0]
    }' > "${json}" 2>/dev/null || {
      # fallback if jq fails
      echo "{\"date\":\"$DATE\",\"generated\":\"$stamp\",\"repo\":\"$HF_REPO\",\"error\":\"status generation degraded\"}" > "${json}"
    }

  # HTML status
  cat > "${page}" <<EOF
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Arkship Discover — Status</title>
<style>
body{font-family:system-ui,sans-serif;margin
