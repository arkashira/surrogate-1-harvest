# vanguard / quality

## 1. Diagnosis
- No client-side router exists; navigation is full-page or broken, breaking deep-linking, back/forward, and iframe embeds.
- Dataset links do not map to URL-driven detail views; sharing or bookmarking a dataset is impossible.
- UI state (selected dataset, filters) is not reflected in the URL, so reloads lose context.
- Missing hash-based navigation (`#/`, `#/datasets`, `#/datasets/:id`) forces full reloads and 404s on direct access.
- No lightweight, dependency-free router abstraction; adding a heavy framework would contradict the existing minimal frontend footprint.

## 2. Proposed change
Create `/opt/axentx/vanguard/frontend/router.js` (new) and update `/opt/axentx/vanguard/frontend/index.html` to:
- include the router script
- convert navigation links to hash-based anchors
- add a minimal content outlet and a dataset detail template
Scope: ~80 lines total; no build step; works with existing static files.

## 3. Implementation

### `/opt/axentx/vanguard/frontend/index.html`
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Datasets</title>
  <style>
    body{font-family:system-ui,Arial,sans-serif;margin:0;padding:1rem;color:#111;background:#f7f7f7}
    nav a{margin-right:1rem;color:#0366d6;text-decoration:none}
    nav a.active{font-weight:700;text-decoration:underline}
    .card{max-width:720px;margin-top:1rem;padding:1rem;background:#fff;border:1px solid #e1e4e8;border-radius:6px}
    .card h2{margin:.25rem 0}
    .meta{color:#6a737d;font-size:.9rem}
    button{cursor:pointer}
  </style>
</head>
<body>
  <nav>
    <a href="#/" data-link>Home</a>
    <a href="#/datasets" data-link>Datasets</a>
  </nav>

  <main id="app" class="card" role="main"></main>

  <!-- dataset detail template (hidden) -->
  <template id="dataset-detail">
    <div>
      <h2 data-field="name"></h2>
      <p class="meta" data-field="meta"></p>
      <p data-field="description"></p>
      <div style="margin-top:1rem">
        <button id="back-to-list">← Back to list</button>
      </div>
    </div>
  </template>

  <script src="./router.js"></script>
</body>
</html>
```

### `/opt/axentx/vanguard/frontend/router.js`
```javascript
// Minimal hash router for Vanguard frontend.
// Supports: #/ , #/datasets , #/datasets/:id
// No dependencies. Uses CDN-fetchable dataset manifest at /datasets.json

(function () {
  const app = document.getElementById('app');
  const template = document.getElementById('dataset-detail');
  const navLinks = document.querySelectorAll('a[data-link]');

  // Mock/sample datasets; replace with real /datasets.json when available
  const DATASETS = [
    { id: 'moc-2026-04-27', name: 'MOC (2026-04-27)', description: 'Most-connected hub snapshot for contextual insights.', meta: 'hub, graph, knowledge-rag' },
    { id: 'mirror-merged-2026-04-29', name: 'Surrogate-1 Mirror Merged', description: 'Enriched parquet shards projected to {prompt,response}. Attribution in filename.', meta: 'surrogate-1, hf-cdn, schema' },
    { id: 'business-research-2026-04-27', name: 'Business Research RAG', description: 'Granite business research + knowledge-rag top-hub insights.', meta: 'business-research, knowledge-rag, graph' }
  ];

  function setActiveNav(path) {
    navLinks.forEach(a => {
      const href = a.getAttribute('href') || '';
      a.classList.toggle('active', href === '#' + path || (path === '' && href === '#/'));
    });
  }

  function renderHome() {
    app.innerHTML = `
      <h1>Vanguard</h1>
      <p class="meta">Quality-focused dataset index and lightweight router.</p>
      <p><a href="#/datasets" data-link>Browse datasets</a></p>
    `;
  }

  async function renderDatasets() {
    // Prefer real manifest; fallback to local DATASETS
    let items = DATASETS;
    try {
      const res = await fetch('/datasets.json', { cache: 'no-cache' });
      if (res.ok) {
        const json = await res.json();
        if (Array.isArray(json)) items = json;
      }
    } catch (e) {
      // ignore; use fallback
    }

    app.innerHTML = `
      <h1>Datasets</h1>
      <p class="meta">Click a dataset to view details. URLs are shareable.</p>
      <ul style="list-style:none;padding:0">
        ${items.map(d => `
          <li style="margin:.5rem 0">
            <a href="#/datasets/${encodeURIComponent(d.id)}" data-link>${escapeHtml(d.name)}</a>
            <span class="meta" style="margin-left:.5rem">${escapeHtml(d.meta || '')}</span>
          </li>
        `).join('')}
      </ul>
    `;
  }

  function renderDataset(id) {
    const item = DATASETS.find(d => d.id === id) || { id, name: id, description: 'Dataset details not found locally.', meta: '' };
    const clone = template.content.cloneNode(true);
    clone.querySelector('[data-field="name"]').textContent = item.name;
    clone.querySelector('[data-field="meta"]').textContent = item.meta;
    clone.querySelector('[data-field="description"]').textContent = item.description;
    clone.querySelector('#back-to-list').addEventListener('click', () => {
      location.hash = '#/datasets';
    });
    app.innerHTML = '';
    app.appendChild(clone);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (m) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[m];
    });
  }

  function router() {
    const hash = location.hash.replace(/^#/, '') || '/';
    const parts = hash.split('/').filter(Boolean); // ['datasets'] or ['datasets','id']

    setActiveNav(hash || '/');

    if (hash === '/' || parts[0] === '') {
      renderHome();
    } else if (parts[0] === 'datasets' && parts.length === 1) {
      renderDatasets();
    } else if (parts[0] === 'datasets' && parts[1]) {
      renderDataset(decodeURIComponent(parts[1]));
    } else {
      app.innerHTML = '<h1>404</h1><p>Not found</p>';
    }
  }

  // Handle navigation via data-link (prevents full reload) and hash changes
  document.addEventListener('click', function (e) {
    const anchor = e.target.closest('a[data-link]');
    if (anchor) {
      e.preventDefault();
      const href = anchor.getAttribute('href');
      if (href && href.startsWith('#')) {
        location.hash = href.replace(/^#/, '#');
      }
    }
  });

  window.addEventListener('hashchange', router);
  window.addEventListener('load', router);

  // Expose for debugging
  window.__vanguard_router = { DATASETS };
})();
```

## 4. Verification
1. Open `/opt/axentx/vanguard/frontend/index.html` in a browser (file:// or served).
2. Click “
