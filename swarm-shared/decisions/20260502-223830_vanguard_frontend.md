# vanguard / frontend

## Final synthesized implementation (strongest parts, resolved contradictions)

**What we keep and why**
- Candidate 1’s zero-dependency hash router + single-file HTML is correct for immediate actionability (<2h, no Node install).  
- Candidate 2’s explicit route map (`/`, `/datasets`, `/studio`, `/hub`) and `DatasetList`/`Hub` pages are correct for discoverability and onboarding (knowledge-rag/graph/hub).  
- Both agree we must embed the HF CDN-bypass `file-list.json` and avoid HF API 429s during browsing/training previews.  
- Candidate 1’s `dev.sh` (Python http.server) is the safest cross-platform dev entrypoint (no Node, no build toolchain). Candidate 2’s Vite is optional for teams that want HMR; we provide both.

**Contradictions resolved in favor of correctness + actionability**
- No Vite by default: requiring Node/Vite adds setup time and fragility for a ~120-line scaffold. Default to zero-dependency static + Python dev server. Offer Vite as opt-in.
- One source of truth for routes: use Candidate 2’s explicit route map (`routes.js` style) but implement in vanilla JS so it remains dependency-free.
- Keep Candidate 1’s CDN-bypass guidance and Lightning training notes (hf_hub_download per file, reuse studios) — they are concrete and correct.
- Place `file-list.json` and `hub-top.json` in `public/` so the static server serves them without auth (CDN-bypass).

---

## 1. Create scaffold (one-time)

```bash
mkdir -p /opt/axentx/vanguard/frontend/src/pages /opt/axentx/vanguard/frontend/public
cd /opt/axentx/vanguard/frontend
```

---

## 2. index.html (SPA shell, canonical mount)

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Frontend</title>
  <meta name="description" content="Top-hub (MOC) + HF CDN-bypass dataset browser for surrogate-1 workflows.">
  <style>
    :root { font-family: system-ui, sans-serif; color: #0f172a; background: #f8fafc; }
    header { padding: 1rem 1.5rem; border-bottom: 1px solid #e2e8f0; background: #fff; }
    main { padding: 1.5rem; max-width: 980px; margin: 0 auto; }
    a { color: #2563eb; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .card { background: #fff; border: 1px solid #e2e8f0; padding: 1rem 1.25rem; border-radius: 8px; margin-bottom: 1rem; }
    pre { background:#0f172a; color:#e6eef8; padding:0.75rem; border-radius:6px; overflow:auto; font-size:0.85rem; }
    .muted { color:#64748b; font-size:0.9rem; }
    .btn { display:inline-block; padding:0.4rem 0.7rem; border-radius:6px; background:#2563eb; color:#fff; font-size:0.85rem; }
    .btn:hover { background:#1d4ed8; text-decoration:none; }
  </style>
</head>
<body>
  <header>
    <nav>
      <a href="#/">Home</a> ·
      <a href="#/hub">Top Hub (MOC)</a> ·
      <a href="#/datasets">Datasets (CDN-bypass)</a> ·
      <a href="#/studio">Lightning Studio</a> ·
      <a href="https://example.com/knowledge-rag" target="_blank" rel="noopener">Knowledge-RAG</a>
    </nav>
  </header>
  <main id="app"></main>

  <script type="module" src="/src/main.js"></script>
</body>
</html>
```

---

## 3. src/main.js (router-first, zero deps)

```js
// Minimal hash router with explicit route map
const app = document.getElementById('app');

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}

async function fetchJSON(path) {
  try {
    const r = await fetch(path);
    if (!r.ok) throw new Error('fetch failed');
    return await r.json();
  } catch {
    return null;
  }
}

// Reusable components (kept tiny)
function card(title, content) {
  return `<div class="card"><h2>${escapeHtml(title)}</h2>${content}</div>`;
}

function fileLink(repo, path) {
  // CDN-bypass direct download (no Authorization header)
  const url = `https://huggingface.co/datasets/${repo}/resolve/main/${encodeURIComponent(path)}`;
  return `<a href="${url}" target="_blank" rel="noopener">${escapeHtml(path)}</a>`;
}

// Route implementations
const routes = {
  '/': () => {
    app.innerHTML = `
      ${card('Top Hub: MOC', `
        <p class="muted">Review the most-connected hub before planning tasks. Tags: #knowledge-rag #graph #hub</p>
        <p><a href="#/hub" class="btn">Open Hub insights</a></p>
      `)}
      ${card('Business Research', `
        <p class="muted">Run market analysis then query top hub and related docs for contextual insights.</p>
        <p><a href="#/datasets">Browse datasets (HF CDN-bypass)</a></p>
      `)}
      ${card('Surrogate-1 Training Notes', `
        <ul class="muted">
          <li>Avoid load_dataset(streaming=true) for heterogeneous repos — use hf_hub_download per file.</li>
          <li>Pre-list file paths once, embed in train.py; Lightning training should CDN-only fetch.</li>
          <li>Reuse running Lightning Studios to save quota; check status before .run().</li>
        </ul>
      `)}
    `;
  },

  '/hub': async () => {
    const hub = await fetchJSON('/hub-top.json');
    app.innerHTML = card('Top Hub (MOC) — Insights', `
      ${hub ? `<pre>${escapeHtml(JSON.stringify(hub, null, 2))}</pre>` : `<p class="muted">No hub-top.json found at /hub-top.json</p>`}
      <p class="muted">Embed hub-top.json (produced by knowledge-rag) to surface top-connected entities and quick links.</p>
    `);
  },

  '/datasets': async () => {
    const list = await fetchJSON('/file-list.json');
    let body = `<p class="muted">Embed file-list.json (from Mac orchestration) to avoid HF API rate limits during browsing/training previews.</p>`;
    if (list && Array.isArray(list.files)) {
      const repo = list.repo || 'datasets/example-repo';
      body += `<ul style="padding-left:1rem;">`;
      for (const f of list.files) {
        body += `<li style="margin-bottom:0.5rem;">${fileLink(repo, f)}</li>`;
      }
      body += `</ul>`;
    } else {
      body += `<p class="muted">No file-list.json found at /file-list.json</p>`;
    }
    body += `<div class="card"><h3>Usage</h3><p class="muted">Place file-list.json in /opt/axentx/vanguard/frontend/public/file-list.json (or serve from CDN).</p>
      <p class="muted">Training script on Lightning should use the embedded list and fetch files via CDN URLs (no Authorization header).</p></div>`;
    app.innerHTML = card('Datasets — CDN-bypass file list', body);
  },

  '/studio': () => {
    app.innerHTML = card('Lightning Studio', `
      <p class="muted">Run and monitor surrogate-1 training in Lightning Studio to
