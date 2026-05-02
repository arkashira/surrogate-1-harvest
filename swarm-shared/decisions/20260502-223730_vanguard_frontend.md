# vanguard / frontend

## Final synthesized implementation (best parts, resolved contradictions)

**Diagnosis (merged & resolved)**  
- Missing canonical frontend entrypoint → new devs cannot orient.  
- No “Top Hub” (MOC) affordance to surface most-connected node + context before planning.  
- No HF CDN-bypass file-list flow → surrogate-1 ingestion/training will hit HF API 429s.  
- No idempotent single-page scaffold for routing/state/actions.  
- No lightweight run for business research + knowledge-rag from UI.  

**Chosen approach**  
- Single-file-first: `/opt/axentx/vanguard/index.html` (self-contained) + optional `app.js`/`style.css`.  
- Use CDN-only assets (htmx + tailwind) to avoid build step while keeping UX reactive.  
- Prefer client-side file-list generation + download (no backend required) to avoid 429s and keep flow Mac-local.  
- Provide clear backend stub (`/api/top-hub`) and CLI runbook commands so pipeline can be wired later.  
- Idempotent: running creation multiple times yields same usable result.

---

### Files to create

#### 1) `/opt/axentx/vanguard/index.html`
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Frontend</title>
  <script src="https://unpkg.com/htmx.org@1.9.12"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="style.css" />
</head>
<body class="min-h-screen bg-slate-50 text-slate-800">
  <header class="top-bar">
    <div class="brand">Vanguard</div>
    <nav>
      <a href="#" data-page="hub" class="nav-link">Top Hub</a>
      <a href="#" data-page="hf" class="nav-link">HF CDN-bypass</a>
      <a href="#" data-page="research" class="nav-link">Research + RAG</a>
    </nav>
  </header>

  <main id="app" class="container" role="main"></main>

  <footer class="footer">
    <small>Frontend shell — orchestrates HF CDN-bypass and top-hub insights</small>
  </footer>

  <script src="app.js"></script>
</body>
</html>
```

#### 2) `/opt/axentx/vanguard/app.js`
```javascript
// Minimal SPA for Vanguard frontend
(function () {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // State
  let fileListCache = null;

  const pages = {
    hub: () => `
      <section class="page" aria-labelledby="hub-title">
        <h1 id="hub-title" class="page-title">Top Hub — MOC</h1>
        <p class="muted">Review the most-connected hub before planning tasks. (Tags: #knowledge-rag #graph #hub)</p>

        <div class="card" id="top-hub-card">
          <h2 class="card-title">MOC (Mission Operations Center)</h2>
          <p id="hub-insight" class="card-text">Loading contextual insight…</p>
          <div class="card-actions">
            <button id="refresh-hub" class="btn">Refresh Insight</button>
          </div>
        </div>

        <div class="note">
          <strong>Pattern:</strong> Always review top-hub docs before planning. Use knowledge-rag to query related nodes.
        </div>
      </section>
    `,

    hf: () => `
      <section class="page" aria-labelledby="hf-title">
        <h1 id="hf-title" class="page-title">HF CDN-bypass — File List</h1>
        <p class="muted">
          Pre-list file paths once (single API call from Mac) and embed in training scripts to avoid HF API 429s.
          CDN downloads (resolve/main/) bypass auth and rate limits.
        </p>

        <div class="card">
          <label class="form-label">Repo (e.g. datasets/owner/repo)</label>
          <input id="hf-repo" class="form-input" placeholder="datasets/owner/repo" />

          <label class="form-label">Folder path (optional)</label>
          <input id="hf-folder" class="form-input" placeholder="batches/mirror-merged/2026-05-02" />

          <div class="row">
            <button id="list-files" class="btn">List files (API → JSON)</button>
            <button id="download-json" class="btn" disabled>Download file-list.json</button>
          </div>

          <div id="hf-status" class="status" aria-live="polite"></div>
        </div>

        <div id="file-list" class="file-list" aria-live="polite"></div>

        <div class="note">
          <strong>Pattern:</strong> Single API call to list_repo_tree(path, recursive=False) from Mac after rate-limit window clears. Save list to JSON. Lightning training uses CDN-only fetches with zero API calls.
        </div>
      </section>
    `,

    research: () => `
      <section class="page" aria-labelledby="research-title">
        <h1 id="research-title" class="page-title">Business Research + Knowledge-RAG</h1>
        <p class="muted">Run market analysis and query top hub + related docs for contextual insights.</p>

        <div class="card">
          <div class="row">
            <button id="run-research" class="btn">Run granite-business-research.sh</button>
            <button id="run-rag" class="btn">Run knowledge-rag (top hub)</button>
          </div>
          <div id="research-output" class="output" aria-live="polite"></div>
        </div>

        <div class="note">
          <strong>Pattern:</strong> After market analysis script, execute knowledge-rag to query top hub and related docs. Tags: #business-research #knowledge-rag #graph
        </div>

        <div class="card" style="margin-top:12px;">
          <h3 class="card-title">Backend stub (for wiring)</h3>
          <p class="card-text">Expose a small backend route to return top-hub insight and run scripts:</p>
          <pre class="code-block">GET /api/top-hub
  → { "hub": "MOC", "insight": "...", "related": [...] }

POST /api/run-script
  { "script": "granite-business-research.sh" }
  → stream or { "ok": true, "log": "..." }

POST /api/run-script
  { "script": "knowledge-rag", "args": ["--top-hub"] }
  → stream or { "ok": true, "result": "..." }</pre>
        </div>
      </section>
    `
  };

  function render(pageKey) {
    const app = $('#app');
    if (!app) return;
    app.innerHTML = pages[pageKey] ? pages[pageKey]() : pages.hub();
    wire(pageKey);
  }

  function wire(pageKey) {
    if (pageKey === 'hub') {
      const btn = $('#refresh-hub');
      if (btn) btn.addEventListener('click', fetchTopHub);
      fetchTopHub();
    }

    if (pageKey === 'hf') {
      $('#list-files').addEventListener('click', listHFFiles);
      $('#download-json').addEventListener('click', downloadFileList);
    }

    if (pageKey === 'research') {
      $('#run-research').addEventListener('click', () => runCommand('granite-business-research.sh', 'research-output'));
      $('#run-rag').addEventListener('click', () => runCommand('knowledge-rag --top-hub', 'research-output'));
    }

    // nav
    $$('.nav-link').forEach(a => {
      a.addEventListener('click', (e) => {
        e.preventDefault();
