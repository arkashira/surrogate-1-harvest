# vanguard / quality

## Final synthesized implementation

**Scope**: Add a minimal, dependency-free hash router and URL-driven state to `/opt/axentx/vanguard/frontend/` so dataset list/detail views, filters, and browser history/bookmarks/iframes work reliably.

### Files to create
- `/opt/axentx/vanguard/frontend/index.html`
- `/opt/axentx/vanguard/frontend/app.js`

### Actions (run once)
```bash
mkdir -p /opt/axentx/vanguard/frontend
```

---

### `/opt/axentx/vanguard/frontend/index.html`
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Datasets</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --text: #e2e8f0;
      --border: #334155;
      --danger: #fb7185;
    }
    *,*::before,*::after { box-sizing: border-box; }
    html,body { height: 100%; margin: 0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); }
    #app { min-height: 100%; display: flex; flex-direction: column; }
    header { padding: 1rem 1.25rem; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
    header h1 { margin: 0; font-size: 1rem; letter-spacing: 0.02em; }
    nav a { margin-left: 1rem; color: var(--muted); text-decoration: none; font-size: 0.875rem; }
    nav a.active { color: var(--accent); }
    main { flex: 1; padding: 1.25rem; max-width: 1000px; margin: 0 auto; width: 100%; }
    .card { background: var(--card); border-radius: 8px; padding: 1rem; margin-bottom: 0.75rem; border: 1px solid var(--border); }
    .card h3 { margin: 0 0 0.5rem 0; font-size: 0.95rem; }
    .card p { margin: 0; color: var(--muted); font-size: 0.85rem; }
    .detail h2 { margin-top: 0; }
    .back { display: inline-block; margin-bottom: 0.75rem; color: var(--accent); cursor: pointer; font-size: 0.85rem; }
    .filters { display: flex; gap: 0.5rem; margin-bottom: 1rem; flex-wrap: wrap; align-items: center; }
    .pill { padding: 0.35rem 0.6rem; border-radius: 999px; background: #334155; color: var(--muted); font-size: 0.75rem; cursor: pointer; border: 1px solid transparent; transition: background .12s, color .12s; }
    .pill:hover { background: #475569; }
    .pill.active { background: var(--accent); color: #0f172a; }
    .empty { color: var(--muted); font-size: 0.85rem; }
    .meta { font-size: 0.75rem; color: var(--muted); margin-top: 0.5rem; }
    .actions { display: flex; gap: 0.5rem; margin-top: 0.75rem; flex-wrap: wrap; }
    .btn { padding: 0.4rem 0.7rem; border-radius: 6px; border: 1px solid var(--border); background: transparent; color: var(--text); cursor: pointer; font-size: 0.8rem; }
    .btn.primary { background: var(--accent); color: #0f172a; border-color: var(--accent); }
    .btn.danger { border-color: rgba(251,113,133,0.4); color: var(--danger); }
    .row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
    input[type="search"], input[type="text"], select { padding: 0.4rem 0.6rem; border-radius: 6px; border: 1px solid var(--border); background: #1e293b; color: var(--text); font-size: 0.85rem; }
    input[type="search"] { width: 100%; }
    .toast { position: fixed; bottom: 1rem; right: 1rem; background: #334155; color: var(--text); padding: 0.6rem 0.9rem; border-radius: 8px; border: 1px solid var(--border); font-size: 0.85rem; z-index: 1000; display: none; }
    .toast.show { display: block; }
    @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
  </style>
</head>
<body>
  <div id="app"></div>
  <div id="toast" class="toast" role="status" aria-live="polite"></div>
  <script src="./app.js"></script>
</body>
</html>
```

---

### `/opt/axentx/vanguard/frontend/app.js`
```javascript
(function () {
  // ---- Minimal dataset store (replace with real API fetch as needed) ----
  const DATASETS = [
    { id: 'moc-2026-04-27', name: 'MOC — Top Hub', desc: 'Most-connected hub for knowledge graph insights.', tags: ['knowledge-rag', 'graph', 'hub'] },
    { id: 'surrogate-1-2026-04-29', name: 'Surrogate-1 Training', desc: 'HF ingestion & Lightning training patterns and fixes.', tags: ['training', 'hf-datasets', 'lightning-ai'] },
    { id: 'opus-pr-reviewer', name: 'Opus PR Reviewer', desc: 'Wrapper script fixes for cron and bash invocation.', tags: ['bash', 'cron', 'opus-pr-reviewer'] },
    { id: 'kaggle-kgat-auth', name: 'Kaggle KGAT Auth', desc: 'Bearer token and kernels/push API fixes.', tags: ['kaggle', 'api', 'auth'] }
  ];

  // ---- Router core ----
  function parseHash() {
    const hash = location.hash || '#/';
    const parts = hash.slice(1).split('/').filter(Boolean); // ['datasets','id']
    const route = parts[0] || 'home';
    const id = parts[1] || null;
    return { raw: hash, route, id, parts };
  }

  function parseSearch() {
    const p = new URLSearchParams(location.search);
    const tags = p.getAll('tag');
    const q = p.get('q') || '';
    const view = p.get('view') || 'list'; // list | detail
    return { tags, q, view };
  }

  function buildSearch({ tags = [], q = '', view } = {}) {
    const sp = new URLSearchParams();
    tags.forEach(t => sp.append('tag', t));
    if (q) sp.set('q', q);
    if (view) sp.set('view', view);
    const s = sp.toString();
    return s ? '?' + s : '';
  }

  function navigateToHash(route, id, searchState) {
    let hash = '#';
    if (route === 'home') hash = '#/';
    else if (route === 'datasets' && !id) hash = '#/datasets
