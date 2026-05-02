# airship / frontend

## Final Decision  
Ship a **single frontend orchestrator** (`airship discover`) that:  

1. Runs **market research** (if available) + **knowledge‑RAG top‑hub query** → tagged insights (JSON).  
2. Calls **HF `list_repo_tree` once per date folder** → emits a **CDN‑only training manifest** (JSON) with `https://huggingface.co/datasets/{repo}/resolve/main/{path}` URLs to bypass API rate limits during surrogate training.  
3. Exposes a **minimal UI/CLI** to view insights, copy CDN URLs, and download the manifest for Lightning Studio.  

Timeboxed to **<2 h**, CLI-first, no backend changes.  

---

## Implementation Plan (≤2 h)

| Step | Time | Concrete Action |
|------|------|-----------------|
| 1 | 15 m | Create orchestrator script at `/opt/axentx/airship/frontend/scripts/airship-discover.sh` (or `bin/airship-discover`). Use `set -euo pipefail`, log to `outputs/discover.log`. |
| 2 | 30 m | Run research: if `granite-business-research.sh` exists and is executable, run it; else stub. Then emit tagged insights JSON (`outputs/insights-{ts}.json`) with top hub from knowledge‑RAG (stub if needed). |
| 3 | 30 m | Generate CDN-only manifest: call HF `list_repo_tree` once per `DATE_FOLDER` (env/config), retry once on 429 with 360 s backoff. Build `outputs/manifest-{date}.json` containing `{path, cdn_url}` for files only. |
| 4 | 20 m | Add lightweight UI: `frontend/pages/Discover.vue` (or `.tsx`) that reads `insights.json` and selected manifest, shows top-hub insight, lists CDN URLs with copy-to-clipboard, and a “Download manifest” button. |
| 5 | 15 m | Wire route `/discover` into dev server/router and add nav link. Ensure `outputs/` is gitignored but created on demand. |
| 6 | 10 m | Smoke test: run script, verify JSON outputs, open `/discover`, copy URL, download manifest. Add short usage note to `frontend/README.md`. |

---

## Code Snippets

### `bin/airship-discover` (or `frontend/scripts/airship-discover.sh`)
```bash
#!/usr/bin/env bash
# airship discover — produce tagged research + CDN-only training manifest
# Tags: #business-research #knowledge-rag #graph #huggingface #cdn #rate-limit-bypass
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUTS="${ROOT}/outputs"
mkdir -p "${OUTPUTS}"

LOG="${OUTPUTS}/discover.log"
exec > >(tee -a "${LOG}") 2>&1

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] airship discover started"

# Config (env overrides)
HF_REPO="${HF_REPO:-datasets/example-repo}"
DATE_FOLDER="${DATE_FOLDER:-$(date -u +%Y-%m-%d)}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

# 1) Business research (if available)
RESEARCH_SCRIPT="${ROOT}/scripts/granite-business-research.sh"
if [[ -x "${RESEARCH_SCRIPT}" ]]; then
  echo "Running business research..."
  "${RESEARCH_SCRIPT}"
else
  echo "No research script found; creating stub insights."
fi

# 2) Knowledge-RAG top-hub insight (stub; replace with real CLI/API)
TOP_HUB="MOC"
INSIGHTS_FILE="${OUTPUTS}/insights-${TS}.json"
cat > "${INSIGHTS_FILE}" <<EOF
{
  "generated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "tags": ["#business-research", "#knowledge-rag", "#graph", "#hub"],
  "top_hub": "${TOP_HUB}",
  "summary": "Top-connected hub is ${TOP_HUB}. Recommended to review before planning tasks.",
  "research_ran": $([[ -x "${RESEARCH_SCRIPT}" ]] && echo true || echo false)
}
EOF
echo "Insights written to ${INSIGHTS_FILE}"

# 3) CDN-only training manifest
MANIFEST_FILE="${OUTPUTS}/manifest-${DATE_FOLDER}.json"

list_files() {
  local token=""
  if [[ -n "${HF_TOKEN:-}" ]]; then
    token="Authorization: Bearer ${HF_TOKEN}"
  fi
  curl -sSf -H "${token}" \
    "https://huggingface.co/api/datasets/${HF_REPO}/tree?path=${DATE_FOLDER}&recursive=false" \
    || return 1
}

MAX_RETRIES=1
RETRY_DELAY=360
attempt=0
tree_json=""
while (( attempt <= MAX_RETRIES )); do
  if (( attempt > 0 )); then
    echo "HF API failed; retry ${attempt}/${MAX_RETRIES} after ${RETRY_DELAY}s..."
    sleep "${RETRY_DELAY}"
  fi
  if tree_json=$(list_files 2>/dev/null); then
    break
  fi
  attempt=$((attempt + 1))
done

if [[ -z "${tree_json:-}" ]]; then
  echo "WARNING: Could not list repo files via HF API. Creating empty manifest."
  tree_json="[]"
fi

echo "${tree_json}" | jq -r '
  map(select(.type == "file")) |
  map({
    path: .path,
    cdn_url: ("https://huggingface.co/datasets/" + $HF_REPO + "/resolve/main/" + .path)
  }) |
  {
    repo: $HF_REPO,
    date_folder: $DATE_FOLDER,
    generated_at: $NOW,
    files: .
  }' \
  --arg HF_REPO "${HF_REPO}" \
  --arg DATE_FOLDER "${DATE_FOLDER}" \
  --arg NOW "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  > "${MANIFEST_FILE}"

echo "CDN manifest written to ${MANIFEST_FILE}"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] airship discover finished"
```

Make executable:
```bash
chmod +x /opt/axentx/airship/frontend/scripts/airship-discover.sh
# or if using bin/airship-discover:
chmod +x bin/airship-discover
```

---

### `frontend/pages/Discover.vue` (minimal)
```vue
<template>
  <div class="discover">
    <h1>Arkship Discover</h1>

    <section v-if="insights" class="insights">
      <h2>Insights</h2>
      <pre>{{ JSON.stringify(insights, null, 2) }}</pre>
    </section>

    <section v-if="manifest" class="manifest">
      <h2>Training Manifest ({{ manifest.date_folder }})</h2>
      <p>CDN-only file list (safe for training — no HF API calls during data loading).</p>
      <ul>
        <li v-for="f in manifest.files" :key="f.path">
          <a :href="f.cdn_url" target="_blank" rel="noopener">{{ f.path }}</a>
          <button @click="copy(f.cdn_url)">Copy URL</button>
        </li>
      </ul>
      <button @click="downloadManifest">Download manifest JSON</button>
    </section>
  </div>
</template>

<script>
export default {
  name: "Discover",
  data() {
    return {
      insights: null,
      manifest: null,
    };
  },
  async mounted() {
    try {
      const iRes = await fetch("/outputs/insights.json").catch(() => null);
      if (iRes && iRes.ok) this.insights = await iRes.json();

      // Pick latest manifest or allow selection; here we try a fixed name.
      const mRes = await fetch("/outputs/manifest-" + new Date().toISOString().slice(0,10) + ".json").catch(() => null);
      if (mRes && mRes.ok) this.manifest = await mRes.json();

