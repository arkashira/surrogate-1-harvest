# vanguard / frontend

### Final Synthesis (single, correct, actionable)

**Diagnosis (resolved)**
- No canonical frontend entrypoint or routing scaffold exists → violates `#knowledge-rag #graph #hub`; new devs cannot orient and surrogate-1 ingestion will hit 429s without a HF CDN-bypass file-list flow.
- Missing top-hub (MOC) review affordance and lightweight orchestration entrypoint for safe local dev/backend triggers.

**Proposed change (single plan)**
Create a minimal canonical SPA entrypoint at `/opt/axentx/vanguard/index.html` with:
- A top-hub (MOC) review card that surfaces contextual insight and links to knowledge-rag.
- A HF CDN-bypass “Pre-list HF files” UI/flow that lists remote files and produces a local JSON embed to avoid 429s during surrogate-1 ingestion.
- A lightweight client-side router (no build step required) so the page is immediately runnable and safe for local dev.
- A small orchestration panel to trigger backend jobs and show status.

**Implementation (concrete, copy-paste ready)**

1) Create `/opt/axentx/vanguard/index.html`

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Knowledge Hub</title>
  <style>
    :root { --bg:#0f172a; --card:#1e293b; --muted:#94a3b8; --accent:#38bdf8; --danger:#ef4444; --success:#22c55e; }
    *{box-sizing:border-box}
    body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,"Helvetica Neue",Arial;background:var(--bg);color:#e2e8f0;line-height:1.5}
    header{padding:1rem 1.5rem;border-bottom:1px solid #334155;display:flex;align-items:center;justify-content:space-between;gap:1rem}
    header h1{margin:0;font-size:1.125rem;color:var(--accent)}
    nav a{margin-left:1rem;color:var(--muted);text-decoration:none;font-size:.875rem}
    nav a.active{color:#fff;font-weight:600}
    main{padding:1.5rem;max-width:980px;margin:0 auto}
    .card{background:var(--card);border:1px solid #334155;border-radius:8px;padding:1rem;margin-bottom:1rem}
    .card h2{margin:0 0 .5rem;font-size:1rem}
    .card p{margin:0 0 .75rem;color:var(--muted);font-size:.875rem}
    .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#0f172a;padding:.5rem .75rem;border-radius:6px;font-size:.875rem;font-weight:600;border:none;cursor:pointer}
    .btn.secondary{background:transparent;border:1px solid #475569;color:#e2e8f0}
    .btn:disabled{opacity:.5;cursor:not-allowed}
    .list{list-style:none;padding:0;margin:0}
    .list li{padding:.5rem .75rem;border-bottom:1px solid #334155;display:flex;align-items:center;justify-content:space-between;gap:1rem;font-size:.875rem}
    .list li:last-child{border-bottom:none}
    .badge{display:inline-block;padding:.125rem .5rem;border-radius:999px;font-size:.75rem;background:#334155;color:#cbd5e1}
    .status{font-size:.875rem;color:var(--muted)}
    .status .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:.5rem}
    .status.ok .dot{background:var(--success)}
    .status.err .dot{background:var(--danger)}
    .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:.875rem;color:#cbd5e1;background:#0b1220;padding:.5rem;border-radius:6px;overflow:auto}
    .orchestration .field{margin-bottom:.75rem;display:flex;gap:.5rem;align-items:center}
    .orchestration input[type="text"]{flex:1;background:#0b1220;border:1px solid #334155;color:#e2e8f0;padding:.5rem;border-radius:6px;font-size:.875rem}
    .hidden{display:none}
  </style>
</head>
<body>
  <header>
    <h1>Vanguard</h1>
    <nav>
      <a href="#/" class="nav-link" data-route="/">Top Hub</a>
      <a href="#/files" class="nav-link" data-route="/files">HF Files</a>
      <a href="#/orchestrate" class="nav-link" data-route="/orchestrate">Orchestrate</a>
    </nav>
  </header>

  <main id="app" role="main"></main>

  <script>
    // Minimal client-side router + HF CDN-bypass flow
    const API = {
      topHub: () => fetch('/api/top-hub').then(r => { if (!r.ok) throw new Error('top-hub failed'); return r.json(); }),
      listHF: (repo, path = '') => fetch(`/api/hf-list?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(path)}`).then(r => { if (!r.ok) throw new Error('hf-list failed'); return r.json(); }),
      createEmbed: (items) => fetch('/api/create-embed', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(items) }).then(r => { if (!r.ok) throw new Error('create-embed failed'); return r.json(); }),
      triggerJob: (job, payload) => fetch('/api/trigger', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job, ...payload }) }).then(r => { if (!r.ok) throw new Error('trigger failed'); return r.json(); })
    };

    function $(sel) { return document.querySelector(sel); }
    function on(ev, sel, fn) { document.addEventListener(ev, e => { if (e.target.matches(sel)) fn(e); }); }

    // Routes
    const routes = {
      '/': topHubView,
      '/files': hfFilesView,
      '/orchestrate': orchestrateView
    };

    function router() {
      const hash = location.hash.slice(1) || '/';
      const view = routes[hash] || notFoundView;
      view();
      // active nav
      document.querySelectorAll('.nav-link').forEach(a => a.classList.toggle('active', a.getAttribute('data-route') === hash));
    }

    // Views
    function render(html) {
      $('#app').innerHTML = html;
    }

    async function topHubView() {
      render(`
        <div class="card">
          <h2>Top Hub (MOC)</h2>
          <p id="topHubDesc">Loading contextual insight…</p>
          <div id="topHubActions"></div>
        </div>
        <div class="card">
          <h2>Quick Actions</h2>
          <p class="status" id="topHubStatus">Checking system…</p>
        </div>
      `);
      try {
        const data = await API.topHub();
        const name = escapeHtml(data.name || 'Unnamed hub');
        const desc = escapeHtml(data.description || 'No description available.');
        $('#topHubDesc').innerHTML = `<strong>${name}</strong> — ${desc}`;
        $('#topHubActions').innerHTML = `
          <a class="btn" href="#/files" title="Browse files to build local embed">Browse HF Files →</a>
        `;

