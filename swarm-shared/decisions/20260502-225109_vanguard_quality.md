# vanguard / quality

## Final Synthesized Implementation

**Diagnosis (resolved)**  
- Missing hash router → full reloads, broken deep-links, no shareable URLs.  
- No URL-synchronized state → reloads lose selection; iframe embeds and bookmarks fail.  
- No dataset detail route → users can’t link to a specific dataset or file.  
- No unknown-route fallback → invalid hashes show blank/console errors.  

**Chosen approach**  
Create `/opt/axentx/vanguard/frontend/index.html` and `/opt/axentx/vanguard/frontend/app.js` with a minimal, dependency-free hash router that:

- Maps `#/` and `#/datasets` → dataset list  
- Maps `#/datasets/:id` → dataset detail (folder or file)  
- Syncs selection to URL and restores from URL  
- Renders a clear 404 for unknown hashes  
- Uses CDN-first links (`resolve/main/...`) to reduce API pressure and support direct downloads  
- Avoids inline handlers; uses event delegation for link interception  

---

**Create directory**
```bash
mkdir -p /opt/axentx/vanguard/frontend
```

---

**index.html**
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Vanguard — Datasets</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <style>
    :root {
      --bg: #f7f8fa; --card: #fff; --muted: #6b7280;
      --accent: #2563eb; --border: #e6e9ee; --danger: #dc2626;
    }
    * { box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
      margin: 0; padding: 1rem; background: var(--bg); color: #111827;
    }
    nav { display:flex; gap:0.75rem; align-items:center; margin-bottom:1rem; flex-wrap:wrap; }
    nav a {
      color: var(--accent); text-decoration:none; padding:0.35rem 0.6rem; border-radius:6px;
      font-weight:600; font-size:0.95rem;
    }
    nav a:hover, nav a.active { background:rgba(37,99,235,0.08); }
    main { max-width:900px; }
    .card {
      background: var(--card); border: 1px solid var(--border); padding: 1rem;
      margin-bottom: 0.75rem; border-radius: 8px; box-shadow: 0 1px 2px rgba(16,24,40,0.03);
    }
    .hidden { display: none !important; }
    pre { background:#f1f5f9; padding:0.6rem; border-radius:6px; overflow:auto; font-size:0.85rem; }
    .muted { color: var(--muted); font-size: 0.9rem; }
    .error { color: var(--danger); }
    .list-empty { color: var(--muted); padding: 1rem 0; }
    .actions { display:flex; gap:0.5rem; flex-wrap:wrap; margin-top:0.5rem; }
    .btn {
      display:inline-block; padding:0.35rem 0.7rem; border-radius:6px; font-size:0.85rem;
      text-decoration:none; border:1px solid var(--border); background:#fff; color:var(--accent);
      cursor:pointer;
    }
    .btn:hover { background:#f0f7ff; }
    .btn-ghost { background:transparent; border-color:transparent; color:var(--muted); }
    .meta { font-size:0.85rem; color:var(--muted); margin-top:0.5rem; }
  </style>
</head>
<body>
  <nav aria-label="Main">
    <a href="#/" data-link>Home</a>
    <a href="#/datasets" data-link>Datasets</a>
  </nav>

  <main id="app" role="main"></main>

  <script src="app.js"></script>
</body>
</html>
```

---

**app.js**
```javascript
// Minimal hash router + dataset UI (dependency-free)
(function () {
  // CONFIG — update to your actual Hugging Face dataset repo
  const REPO = 'your-org/your-dataset-repo'; // e.g. 'databricks/databricks-dolly-15k'
  const API_ROOT = 'https://huggingface.co/api/datasets';
  const CDN_ROOT = 'https://huggingface.co/datasets';

  const app = document.getElementById('app');

  /* ---- Utilities ---- */
  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, function (m) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[m];
    });
  }

  function qs(selector, ctx = document) {
    return ctx.querySelector(selector);
  }

  function on(el, type, selector, handler) {
    el.addEventListener(type, function (e) {
      const target = e.target.closest(selector);
      if (target && this.contains(target)) handler.call(target, e);
    });
  }

  function attachRouterLinks(root = document) {
    // Intercept data-link clicks and navigate via hash
    on(root, 'click', 'a[data-link]', function (e) {
      e.preventDefault();
      const href = this.getAttribute('href');
      if (href && href.startsWith('#')) {
        navigate(href.slice(1)); // strip leading '#'
      } else {
        navigate(href || '/');
      }
    });
  }

  /* ---- Router core ---- */
  function parseRoute() {
    const raw = (location.hash || '#/').replace(/^#/, '');
    const parts = raw.split('/').filter(Boolean); // ['datasets', 'path/to/ds']
    const base = parts.length ? parts[0] : '/';
    const rest = parts.slice(1).join('/');
    return { raw, base, id: rest || null };
  }

  function navigate(path) {
    // Accepts either "datasets/abc" or "#/datasets/abc"
    const next = path.startsWith('#') ? path : '#' + (path.startsWith('/') ? path : '/' + path);
    // Avoid redundant pushes that spam history
    const target = next.replace(/^#/, '');
    const current = (location.hash || '#/').replace(/^#/, '');
    if (target !== current) location.hash = next;
    else router(); // force render if same (useful for re-mount)
  }

  function setActiveNav() {
    const { base } = parseRoute();
    document.querySelectorAll('nav a[data-link]').forEach(a => {
      const href = (a.getAttribute('href') || '').replace(/^#/, '');
      const isHome = !href || href === '/';
      if ((isHome && base === '/') || href === base || href === base.split('/')[0]) {
        a.classList.add('active');
      } else {
        a.classList.remove('active');
      }
    });
  }

  /* ---- Pages ---- */
  function renderHome() {
    app.innerHTML = `
      <h1>Vanguard</h1>
      <p class="muted">Lightweight dataset browser using CDN-first access to avoid HF API rate limits during inspection.</p>
      <div class="card">
        <h3>Quick actions</h3>
        <p><a href="#/datasets" data-link>Browse datasets</a></p>
        <p class="meta">Tip: Each folder or parquet file in the repo is treated as a dataset or dataset file.</p>
      </div>
    `;
    attachRouterLinks(app);
  }

  function renderNotFound() {
    app.innerHTML = `
      <h1>4
