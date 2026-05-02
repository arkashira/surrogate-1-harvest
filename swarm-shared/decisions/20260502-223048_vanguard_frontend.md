# vanguard / frontend

## 1. Diagnosis
- No frontend discovery entrypoint (index.html or SPA mount) exists → violates `#knowledge-rag #graph #hub` pattern; new devs cannot orient quickly.
- Missing canonical top-hub review step in frontend planning; no UI affordance to surface MOC/contextual insights.
- No CDN-bypass file-list generator for HF datasets in frontend tooling → future surrogate-1 training integrations will hit 429s when listing files from browser/electron contexts.
- No Lightning Studio reuse guard in frontend orchestration UI (if present) → risk of burning quota by recreating running studios.
- Missing Bash shebang + executable hygiene in any frontend wrapper scripts (e.g., dev/preview scripts) → potential cron/CI failures (`#bash #script-error`).

## 2. Proposed change
Create a lightweight frontend entrypoint and context bootstrap:
- File: `/opt/axentx/vanguard/index.html` (new)
- File: `/opt/axentx/vanguard/src/main.js` (new)
- File: `/opt/axentx/vanguard/src/app.css` (new)
- Add a small CLI helper: `/opt/axentx/vanguard/scripts/list-hf-files.sh` to generate CDN-bypass file lists (respects `#huggingface #cdn #rate-limit-bypass #training`).
- Scope: ~120 lines total; <2h to ship.

## 3. Implementation

```bash
# Create project structure
mkdir -p /opt/axentx/vanguard/{src,scripts,styles}
```

### index.html
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Axentx</title>
  <link rel="stylesheet" href="./src/app.css" />
</head>
<body>
  <div id="app">
    <header class="top-hub">
      <h1>Vanguard</h1>
      <p class="subtitle">Axentx product family — frontend</p>
      <div id="hub-insight" class="card">
        <strong>Top hub:</strong> <span id="hub-name">MOC</span>
        <p id="hub-desc">Review most-connected docs before planning tasks. (Tag: #knowledge-rag #graph #hub)</p>
      </div>
    </header>

    <section class="tools">
      <div class="card">
        <h2>HF CDN-bypass file list</h2>
        <p>Generate file list for surrogate-1 training to avoid API rate limits.</p>
        <button id="gen-list">Generate list (dry-run)</button>
        <pre id="output" class="output" aria-live="polite"></pre>
      </div>

      <div class="card">
        <h2>Lightning Studio reuse</h2>
        <p>Orchestration note: reuse running studios to save quota.</p>
        <button id="check-studios">Check studios (stub)</button>
        <pre id="studio-output" class="output"></pre>
      </div>
    </section>

    <footer>
      <small>Patterns applied: #knowledge-rag #cdn #rate-limit-bypass #lightning-ai</small>
    </footer>
  </div>

  <script type="module" src="./src/main.js"></script>
</body>
</html>
```

### src/app.css
```css
:root{
  --bg:#0b0f19;
  --card:#0f141e;
  --accent:#00d9a0;
  --muted:#6b7280;
  --text:#e6eef6;
  --radius:10px;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
}

*{box-sizing:border-box}
html,body{height:100%;margin:0;background:var(--bg);color:var(--text)}
#app{max-width:900px;margin:0 auto;padding:24px}
.top-hub{margin-bottom:20px}
.subtitle{color:var(--muted);margin:4px 0 16px}
.card{
  background:var(--card);
  border:1px solid rgba(255,255,255,0.04);
  padding:16px;border-radius:var(--radius);
  margin-bottom:12px;
}
#hub-insight{border-left:3px solid var(--accent)}
button{
  background:var(--accent);
  color:#000;
  border:none;padding:8px 14px;border-radius:6px;
  cursor:pointer;font-weight:600;
}
button:hover{opacity:0.9}
.output{
  background:#07090f;padding:12px;border-radius:6px;
  max-height:220px;overflow:auto;font-size:13px;color:#9ca3af;margin-top:8px;
  white-space:pre-wrap;word-break:break-all;
}
footer{margin-top:18px;color:var(--muted);font-size:12px}
```

### src/main.js
```javascript
// Minimal frontend behaviors + CDN-bypass guidance.
// Note: Heavy HF listing should be done server-side/CLI (see scripts/list-hf-files.sh).
// This UI only triggers dry-run outputs and documents the pattern.

const $ = (sel) => document.querySelector(sel);

$('#gen-list').addEventListener('click', () => {
  const out = $('#output');
  out.textContent = `# CDN-bypass file list (dry-run)
# Run scripts/list-hf-files.sh to generate real list.
# Pattern: list_repo_tree(path, recursive=False) once, save JSON, embed in train.py.
# During Lightning training, use CDN URLs only:
#   https://huggingface.co/datasets/{repo}/resolve/main/{path}
# No Authorization header required -> bypasses /api/ rate limits.
#
# Example generated entry:
# {
#   "date": "2026-04-29",
#   "files": [
#     "batches/mirror-merged/2026-04-29/slug-abc.parquet",
#     "batches/mirror-merged/2026-04-29/slug-def.parquet"
#   ]
# }
`;
});

$('#check-studios').addEventListener('click', () => {
  const out = $('#studio-output');
  out.textContent = `# Lightning Studio reuse (stub)
# Recommended pattern (Python/Lightning SDK):
#
# from lightning import Teamspace, Studio, Machine
# for s in Teamspace.studios:
#   if s.name == "vanguard-train" and s.status == "Running":
#     studio = s; break
# else:
#   studio = Studio.create(name="vanguard-train", machine=Machine.L40S, cluster="lightning-public-prod")
#
# Before each .run(), check status and restart if stopped (idle timeout kills training).
`;
});
```

### scripts/list-hf-files.sh
```bash
#!/usr/bin/env bash
# Generate HF CDN-bypass file list for surrogate-1 training.
# Usage: bash scripts/list-hf-files.sh <repo> <date_folder> > file-list.json
#
# Pattern: Avoid list_repo_files recursive on big repos. Use list_repo_tree per folder.
# After 429: wait 360s before retry.
# This script requires HF_TOKEN in environment for API calls (listing). CDN downloads do not require token.

set -euo pipefail

REPO="${1:-datasets/your-org/your-repo}"
DATE="${2:-2026-04-29}"
API="${HF_API:-https://huggingface.co/api}"

# Use HF API to list tree (non-recursive) for the date folder.
# If rate-limited, retry after delay (simple retry).
retry() {
  local n=0 max=5
  local delay=10
  while ! "$@"; do
    n=$((n+1))
    if [ "$n" -ge "$max" ]; then
      echo "ERROR: Command failed after $max attempts: $*" >&2
      return
