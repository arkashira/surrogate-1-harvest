# airship / frontend

## Final Decision
Ship a **lightweight, self-contained frontend orchestrator** (`airship discover`) that:

1. Runs market research + knowledge-RAG to produce tagged insights (JSON).
2. Lists HF dataset paths **once** and emits a **CDN-only training manifest** (JSON) so training never hits HF API rate-limits.
3. Exposes a minimal, copy-pasteable UI/CLI to view insights and download the manifest.

This removes HF API rate-limit risk during training and surfaces contextual research in <2h.

---

## Implementation Plan (≤2h)

| Step | Owner | Time | Details |
|------|-------|------|---------|
| 1. Scaffold frontend entrypoint | me | 15m | Add `airship/discover/index.html` + `styles.css` + `app.js` |
| 2. CLI orchestrator (bash) | me | 20m | Add `bin/airship-discover` (shebang, executable) to run research + manifest generation |
| 3. Market research + top-hub insight | me | 20m | Wrap `granite-business-research.sh` → run `knowledge-rag` query for top hub (e.g., MOC) |
| 4. HF pre-list + CDN manifest | me | 25m | Single `list_repo_tree` call → save `file-list.json`; generate `training-manifest.json` with CDN URLs |
| 5. UI to render insights + manifest | me | 30m | Render tags, top-hub, and downloadable manifest; include copy-to-clipboard for CLI usage |
| 6. Polish & test locally | me | 20m | Validate bash script exit codes, UI loads, manifest downloads |

Total: ~2h.

---

## Code Snippets

### 1) CLI orchestrator (`bin/airship-discover`)
```bash
#!/usr/bin/env bash
# bin/airship-discover
# Usage: ./bin/airship-discover [--output-dir ./airship/discover/output]
# Ensures bash invocation and proper environment.

set -euo pipefail
export SHELL=/bin/bash

OUTPUT_DIR="${1:-./airship/discover/output}"
mkdir -p "$OUTPUT_DIR"

echo "=== Arkship Discover ==="
echo "Output: $OUTPUT_DIR"

# 1) Business research
if command -v granite-business-research.sh >/dev/null 2>&1; then
  echo "Running market analysis..."
  granite-business-research.sh > "$OUTPUT_DIR/market-analysis.json" || true
fi

# 2) Knowledge RAG — top hub insight (prefer MOC pattern)
if command -v knowledge-rag >/dev/null 2>&1; then
  echo "Querying top hub..."
  knowledge-rag query "top hub and most-connected doc (e.g., MOC)" > "$OUTPUT_DIR/top-hub-insight.json" || true
fi

# 3) HF pre-list (single API call) -> CDN manifest
# Requires: pip install huggingface-hub (or use local script)
HF_REPO="${HF_REPO:-datasets/axentx/surrogate-mirror}"
HF_FOLDER="${HF_FOLDER:-batches/mirror-merged}"
MANIFEST="$OUTPUT_DIR/training-manifest.json"

echo "Listing HF repo tree (non-recursive) for $HF_FOLDER ..."
python3 - <<PY > "$OUTPUT_DIR/file-list.json" 2>"$OUTPUT_DIR/hf-list.log" || true
import os, json, sys
from huggingface_hub import list_repo_tree

repo = os.getenv("HF_REPO", "datasets/axentx/surrogate-mirror")
folder = os.getenv("HF_FOLDER", "batches/mirror-merged")
try:
    tree = list_repo_tree(repo=repo, path=folder, recursive=False)
    files = [f.rfilename for f in tree if f.rfilename]
except Exception as e:
    # Fallback: minimal placeholder so training can proceed with CDN-only URLs pattern
    files = []
    sys.stderr.write(f"HF list failed (may hit rate-limit): {e}\\n")

print(json.dumps({"files": files, "repo": repo, "folder": folder}))
PY

# Build CDN-only manifest (zero API calls during training)
python3 - <<PY
import json, os
HF_REPO = os.getenv("HF_REPO", "datasets/axentx/surrogate-mirror")
with open("$OUTPUT_DIR/file-list.json") as f:
    data = json.load(f)
files = data.get("files", [])
manifest = {
    "repo": HF_REPO,
    "folder": os.getenv("HF_FOLDER", "batches/mirror-merged"),
    "strategy": "cdn-only",
    "note": "Use resolve/main/ URLs to bypass HF API rate-limits during training",
    "files": [
        {
            "path": p,
            "cdn_url": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{p}"
        }
        for p in files
    ]
}
with open("$MANIFEST", "w") as out:
    json.dump(manifest, out, indent=2)
print(f"Manifest written to $MANIFEST ({len(files)} files)")
PY

echo "Done. Outputs in $OUTPUT_DIR"
```

Make executable:
```bash
chmod +x bin/airship-discover
```

---

### 2) Frontend UI (`airship/discover/index.html`)
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Arkship Discover</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <link rel="stylesheet" href="styles.css" />
</head>
<body>
  <main class="container">
    <h1>Arkship Discover</h1>
    <p class="lead">Run market research, top-hub insights, and generate a CDN-only HF training manifest (bypasses API rate-limits).</p>

    <section class="card">
      <h2>Run discovery</h2>
      <pre class="cmd">./bin/airship-discover ./output</pre>
      <button id="runBtn">Run (server-side)</button>
      <small id="runStatus"></small>
    </section>

    <section class="card" id="insightsCard" hidden>
      <h2>Top Hub Insight</h2>
      <pre id="topHubEl">—</pre>
    </section>

    <section class="card" id="manifestCard" hidden>
      <h2>Training Manifest (CDN-only)</h2>
      <p>Download and use in Lightning training to avoid HF API calls during data loading.</p>
      <div class="actions">
        <button id="downloadBtn">Download manifest.json</button>
        <button id="copyBtn">Copy CDN snippet</button>
      </div>
      <pre id="manifestEl" class="small">—</pre>
    </section>
  </main>

  <script src="app.js"></script>
</body>
</html>
```

---

### 3) Styles (`airship/discover/styles.css`)
```css
:root{
  --bg:#0f1724;
  --card:#0b1220;
  --accent:#22d3ee;
  --muted:#64748b;
  --text:#e2e8f0;
}
*{box-sizing:border-box}
body{
  margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,"Helvetica Neue",Arial;
  background:var(--bg);color:var(--text);padding:24px;
}
.container{max-width:900px;margin:0 auto}
.lead{color:var(--muted);margin-top:-8px;margin-bottom:24px}
.card{
  background:var(--card);border:1px solid #1e293b;padding:16px;border-radius:8px;
  margin-bottom:16px;
}
h2{margin:0 0 8px;font-size:16px}
.cmd{
  background:#071024;padding:12px;border-radius:6px;overflow:auto;color:#93c5fd;font-size:13px;
}
button{
  background:var(--accent);color:#022033;border:none;padding:8px 14
