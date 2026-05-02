# airship / frontend

### Final Synthesized Implementation Plan  
*(Combines strongest parts from both candidates; resolves contradictions in favor of correctness + concrete actionability)*

---

**Decision (unanimous):**  
Ship a single frontend CLI orchestrator `airship discover` that:

- Runs market research + knowledge-RAG top-hub query and emits **tagged, typed insights (JSON)**.  
- Calls HF `list_repo_tree` **once per date folder** and embeds file list into a **static manifest (CDN-only training path)**.  
- Produces a **static status page** (`status/index.html`) + **JSON API** (`status/api/status.json`) for downstream UIs.  
- Is **cron-safe** (proper shebang, executable, `SHELL=/bin/bash` in crontab).  
- Can be delivered in **≤2h**.

---

### Implementation Plan (≤2h)

| Step | Owner | Time | Details |
|------|-------|------|---------|
| 1. Create `airship` frontend entrypoint | me | 15m | `/opt/axentx/airship/frontend/bin/airship` (bash orchestrator) |
| 2. Implement `discover` command | me | 45m | - market research script detection + run<br>- knowledge-RAG top-hub query (stub → JSON)<br>- HF `list_repo_tree` → `manifest.json` (CDN-only URLs)<br>- static status page (`status/index.html` + `status/api/status.json`) |
| 3. Cron-safe packaging | me | 15m | shebang, `chmod +x`, crontab line with `SHELL=/bin/bash` |
| 4. Smoke test | me | 30m | run `./airship discover`, verify outputs, ensure no interactive prompts |
| 5. Docs + README update | me | 15m | usage, cron example, CDN bypass note |

---

### Code Snippets

#### 1. `/opt/axentx/airship/frontend/bin/airship`

```bash
#!/usr/bin/env bash
# airship — frontend orchestrator (discover + status)
# Usage: ./airship discover
# Cron-safe: ensure SHELL=/bin/bash in crontab

set -euo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly OUTPUT_DIR="${PROJECT_ROOT}/dist"
readonly STATUS_DIR="${PROJECT_ROOT}/status"
readonly API_DIR="${STATUS_DIR}/api"

log() {
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*"
}

fail() {
  log "ERROR: $*"
  exit 1
}

ensure_tools() {
  local tool
  for tool in jq curl; do
    command -v "${tool}" >/dev/null 2>&1 || fail "required tool not found: ${tool}"
  done
}

# ---- Knowledge-RAG helpers ----
run_market_research() {
  local out_file="${1}"
  local script_path="${PROJECT_ROOT}/scripts/granite-business-research.sh"

  if [[ -x "${script_path}" ]]; then
    log "Running market research: ${script_path}"
    if "${script_path}" > "${out_file}.raw" 2>&1; then
      # normalize to tagged insights
      jq -n \
        --arg source "granite-business-research" \
        --arg ts "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
        --slurpfile raw "${out_file}.raw" \
        '{
          source: $source,
          ts: $ts,
          tags: ["#business-research", "#knowledge-rag", "#graph"],
          insights: ($raw | map(select(length > 0)))
        }' > "${out_file}" || true
    else
      log "WARN: market research script exited non-zero; continuing"
      jq -n '{source:"granite-business-research",ts:now,tags:["#business-research","#knowledge-rag","#graph"],insights:[],error:"script failed"}' > "${out_file}"
    fi
  else
    log "WARN: market research script not found or not executable: ${script_path}"
    jq -n '{source:"granite-business-research",ts:now,tags:["#business-research","#knowledge-rag","#graph"],insights:[],note:"script missing"}' > "${out_file}"
  fi
}

run_knowledge_rag_top_hub() {
  local out_file="${1}"
  # Placeholder: integrate with knowledge-rag CLI / API when available.
  # Per pattern: review most-connected hub (e.g., "MOC") before planning tasks
  jq -n \
    --arg hub "MOC" \
    --arg ts "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    '{
      source: "knowledge-rag",
      ts: $ts,
      tags: ["#knowledge-rag", "#graph", "#hub"],
      top_hub: $hub,
      note: "stub — integrate with knowledge-rag query",
      insights: [
        "Review most-connected hub (" + $hub + ") before planning tasks"
      ]
    }' > "${out_file}"
}

# ---- HF manifest (CDN-only) ----
generate_hf_manifest() {
  local repo="${1:-"datasets/example"}"
  local folder="${2:-""}"
  local out_file="${3}"
  local api_base="https://huggingface.co/api"
  local max_retries=3
  local retry_delay=60

  log "Listing HF repo tree (non-recursive): ${repo} path='${folder}'"
  local attempt=0
  local res
  while (( attempt < max_retries )); do
    res="$(curl -fsSL --retry 2 --retry-delay 5 "${api_base}/datasets/${repo}/tree?path=${folder}&recursive=false" 2>/dev/null)" && break
    attempt=$((attempt + 1))
    log "HF API attempt ${attempt}/${max_retries} failed; waiting ${retry_delay}s"
    sleep "${retry_delay}"
  done

  if [[ -z "${res:-}" ]]; then
    fail "HF API unavailable after ${max_retries} attempts"
  fi

  # Detect rate-limit 429 (should be avoided by non-recursive calls)
  if echo "${res}" | jq -e '.error // empty' >/dev/null 2>&1; then
    log "WARN: HF API returned error: ${res}"
  fi

  # Build manifest with CDN-only download URLs (bypass /api/ auth checks)
  echo "${res}" | jq -c --arg repo "${repo}" '
    {
      generated_at: now,
      repo: $repo,
      strategy: "cdn-only",
      note: "CDN URLs bypass /api/ rate limits; use resolve/main/ paths",
      files: (
        [.[] | select(.type == "file")] |
        map({
          path: .path,
          size: .size,
          cdn_url: ("https://huggingface.co/datasets/" + $repo + "/resolve/main/" + .path)
        })
      )
    }' > "${out_file}"
}

# ---- Status page + JSON API ----
build_status_page() {
  local insights_file="${1}"
  local manifest_file="${2}"
  local html_out="${STATUS_DIR}/index.html"
  local json_out="${API_DIR}/status.json"

  mkdir -p "${API_DIR}"

  # Aggregate payload
  local insights_json manifest_json
  insights_json="$(cat "${insights_file}")"
  manifest_json="$(cat "${manifest_file}")"

  jq -n \
    --argjson insights "${insights_json}" \
    --argjson manifest "${manifest_json}" \
    --arg ts "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    '{
      generated_at: $ts,
      insights: $insights,
      manifest: $manifest
    }' > "${json_out}"

  # Minimal static HTML
  cat > "${html_out}" <<EOF
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Airship Status</title>
<style>body{font-family:sans-serif;margin:2rem}</style>
</head>
<body>

