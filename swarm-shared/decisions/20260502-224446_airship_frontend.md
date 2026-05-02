# airship / frontend

## Highest-value incremental improvement (<2h)

**Ship `airship discover` frontend orchestrator** that:
1. Runs market research + knowledge-RAG top-hub query → tagged insights (JSON).
2. Calls HF `list_repo_tree` once (per date folder) → saves `manifest.json`.
3. Uses CDN-only fetches during training (zero HF API calls).
4. Emits static status page (`status.html`) for quick visibility.

This unifies the known patterns (business-research, knowledge-rag, hub-first, HF CDN bypass, pre-list manifest) into one CLI the frontend can invoke and display.

---

## Implementation plan

1. **Create orchestrator script**  
   - `/opt/axentx/airship/scripts/airship-discover.sh`  
   - Bash shebang, executable, `set -euo pipefail`, `SHELL=/bin/bash` friendly.
   - Steps:
     - If `granite-business-research.sh` exists → run it.
     - Run knowledge-RAG query for top hub (e.g., MOC) → `insights.json`.
     - Run HF `list_repo_tree` (single call) for configured date folder → `manifest.json`.
     - Generate `status.html` with latest timestamps + counts + top insights.
     - Exit 0 on success, non-zero on failure (cron-safe).

2. **Add lightweight Python helper** (optional but recommended)  
   - `/opt/axentx/airship/scripts/build_manifest.py`  
   - Uses HF Hub REST (`/tree`) to list files (no `list_repo_files` recursion).  
   - Accepts repo, date folder → outputs `manifest.json` with CDN URLs.

3. **Add status page template**  
   - `/opt/axentx/airship/static/status.html` (generated into `public/` or served as artifact).

4. **Wire into frontend**  
   - Add simple status route/page that reads `public/status.html` or `status.json` and renders insights + manifest summary.

5. **Make cron-safe**  
   - Ensure script is idempotent, writes to timestamped artifacts, keeps last-success symlink.

---

## Code snippets

### 1) `scripts/airship-discover.sh`

```bash
#!/usr/bin/env bash
# airship-discover.sh
# Orchestrator: market research + knowledge-RAG + HF manifest + status page
# Cron-safe, idempotent, CDN-only downstream.

set -euo pipefail
export SHELL=/bin/bash

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${BASE_DIR}/public"
MANIFEST_FILE="${OUT_DIR}/manifest.json"
INSIGHTS_FILE="${OUT_DIR}/insights.json"
STATUS_FILE="${OUT_DIR}/status.html"
LAST_SUCCESS="${OUT_DIR}/.last_success"

mkdir -p "${OUT_DIR}"

log() {
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*"
}

run_market_research() {
  local script="${BASE_DIR}/scripts/granite-business-research.sh"
  if [[ -x "${script}" ]]; then
    log "Running market research..."
    bash "${script}" > "${OUT_DIR}/market_research.json" || log "WARN: market research failed"
  else
    log "No market research script found, skipping."
  fi
}

run_knowledge_rag() {
  # Placeholder: call your knowledge-RAG CLI or API to query top hub (e.g., MOC).
  # Expected to output tagged insights JSON.
  local script="${BASE_DIR}/scripts/knowledge-rag-query.sh"
  if [[ -x "${script}" ]]; then
    log "Querying knowledge-RAG top hub..."
    bash "${script}" --top-hub > "${INSIGHTS_FILE}" || {
      log "WARN: knowledge-RAG query failed, producing minimal insights"
      echo '{"top_hub":"MOC","tags":["#knowledge-rag","#graph","#hub"],"insights":[]}' > "${INSIGHTS_FILE}"
    }
  else
    log "No knowledge-RAG script found, producing minimal insights"
    echo '{"top_hub":"MOC","tags":["#knowledge-rag","#graph","#hub"],"insights":[]}' > "${INSIGHTS_FILE}"
  fi
}

build_hf_manifest() {
  # Uses HF REST tree API (no auth for public repos) to avoid API rate limits.
  # Configurable via env: HF_REPO (e.g., datasets/your-org/your-repo) + HF_DATE_FOLDER
  local repo="${HF_REPO:-datasets/example/surrogate-data}"
  local date_folder="${HF_DATE_FOLDER:-$(date -u +"%Y-%m-%d")}"
  local api_url="https://huggingface.co/api/datasets/${repo}/tree/${date_folder}?recursive=false"

  log "Fetching HF tree for repo=${repo} folder=${date_folder}..."
  if curl -fsSL "${api_url}" > "${OUT_DIR}/.tree_raw.json"; then
    # Transform to manifest with CDN URLs
    python3 "${BASE_DIR}/scripts/build_manifest.py" \
      --tree "${OUT_DIR}/.tree_raw.json" \
      --repo "${repo}" \
      --folder "${date_folder}" \
      --out "${MANIFEST_FILE}"
    log "Manifest written to ${MANIFEST_FILE}"
  else
    log "WARN: HF tree fetch failed, producing empty manifest"
    echo '{"repo":"'${repo}'","folder":"'${date_folder}'","files":[],"cdn_base":"https://huggingface.co/datasets"}' > "${MANIFEST_FILE}"
  fi
}

generate_status_page() {
  local now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  local top_hub="unknown"
  local insight_count=0
  local file_count=0

  if [[ -f "${INSIGHTS_FILE}" ]]; then
    top_hub=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('top_hub','unknown'))" "${INSIGHTS_FILE}" 2>/dev/null || echo "unknown")
    insight_count=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(len(d.get('insights',[])))" "${INSIGHTS_FILE}" 2>/dev/null || echo 0)
  fi

  if [[ -f "${MANIFEST_FILE}" ]]; then
    file_count=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(len(d.get('files',[])))" "${MANIFEST_FILE}" 2>/dev/null || echo 0)
  fi

  cat > "${STATUS_FILE}" <<EOF
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Arkship Discover — Status</title>
<style>
body{font-family:system-ui,sans-serif;margin:2rem;color:#111}
.card{border:1px solid #e2e8f0;padding:1rem;border-radius:8px;margin-bottom:1rem;max-width:720px}
.kv{display:flex;justify-content:space-between}
.tag{display:inline-block;background:#e2e8f0;padding:0.125rem 0.5rem;border-radius:4px;font-size:0.8rem;margin-right:0.25rem}
</style>
</head>
<body>
<h1>Arkship Discover — Status</h1>

<div class="card">
  <h2>Last run</h2>
  <div class="kv"><span>Timestamp (UTC)</span><strong>${now}</strong></div>
</div>

<div class="card">
  <h2>Top hub</h2>
  <div class="kv"><span>Hub</span><strong>${top_hub}</strong></div>
  <div style="margin-top:0.5rem">
    <span class="tag">#knowledge-rag</span><span class="tag">#graph</span><span class="tag">#hub</span>
  </div>
</div>

<div class="card">
  <h2>Insights</h2>
  <div class="kv"><span>Count</span><strong>${insight_count}</strong></div>
</div>

<div class="card">
  <h2>Manifest</h2>
  <div class="kv"><span>Files listed</span><strong>${file_count}</
