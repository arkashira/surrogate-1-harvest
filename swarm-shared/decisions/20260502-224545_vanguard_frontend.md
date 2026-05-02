# vanguard / frontend

## Final Synthesis (Correct + Actionable)

**Core diagnosis (merged, de-duplicated, prioritized)**
- No client-side router exists; navigation is full-page or broken, which breaks deep-linking, back/forward, and iframe embeds.
- Dataset links do not map to a URL-driven detail view (`#/datasets/:id`), so sharing or refreshing loses context.
- No route-level loading/error states make dataset fetch failures opaque and harm perceived performance.
- Hardcoded repo/folder assumptions and missing CDN-bypass pattern will cause brittle previews and unnecessary HF API calls.

**Chosen approach**
- Add a minimal, dependency-free hash router and route-to-view mapping.
- Wire dataset detail view with loading/error states and a CDN-bypass pattern (single tree API call, then direct CDN URLs).
- Keep it framework-free and small enough to drop into the existing codebase.

**Concrete implementation**

1) Create `/opt/axentx/vanguard/src/router.js`

```js
// Lightweight hash router for vanguard frontend
// Routes: #/ , #/datasets , #/datasets/:id

const Routes = {
  '/': () => window.renderHome?.(),
  '/datasets': () => window.renderDatasetList?.(),
  '/datasets/:id': (params) => window.renderDatasetDetail?.(params.id)
};

function parseHash() {
  const hash = (location.hash || '#/').slice(1);
  const parts = hash.split('/').filter(Boolean); // ['datasets','abc']
  const route = `/${parts[0] || ''}`;
  const id = parts[1];
  return { route: route === '/' ? '/' : route, id, raw: hash };
}

function renderNotFound() {
  const app = document.getElementById('app');
  if (app) {
    app.innerHTML = `
      <div class="card">
        <h2>Not found</h2>
        <p>The page you requested does not exist.</p>
        <p><a href="#/" data-link>Go home</a></p>
      </div>
    `;
    attachLinks();
  }
}

function router() {
  const { route, id } = parseHash();
  const handler = Routes[route];
  if (handler) {
    handler({ id });
  } else {
    renderNotFound();
  }
}

function attachLinks() {
  // Intercept internal links with [data-link] to avoid full reloads
  document.querySelectorAll('a[data-link]').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      const href = a.getAttribute('href');
      if (href && href.startsWith('#')) {
        location.hash = href.slice(1) || '/';
      }
    });
  });
}

// Mount router
window.addEventListener('hashchange', router);
window.addEventListener('load', router);
```

2) Create `/opt/axentx/vanguard/src/main.js` (entry)

```js
// Entry for vanguard frontend router + dataset previews
const API_ROOT = 'https://huggingface.co/datasets';
const REPO = 'your-org/your-dataset-repo'; // <-- replace with actual repo

const app = document.getElementById('app');

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Single tree call per folder; returns CDN-ready file list
async function fetchFileList(dateFolder) {
  try {
    const res = await fetch(
      `https://huggingface.co/api/datasets/${REPO}/tree?path=${encodeURIComponent(dateFolder)}&recursive=false`
    );
    if (!res.ok) throw new Error(`HF tree API failed: ${res.status}`);
    const items = await res.json();
    return items
      .filter((i) => i.type === 'file')
      .map((i) => ({
        path: i.path,
        cdn: `${API_ROOT}/${REPO}/resolve/main/${encodeURIComponent(i.path)}`
      }));
  } catch (err) {
    console.error(err);
    return [];
  }
}

// Optional: list top-level date folders (if needed for UI)
async function listTopFolders() {
  try {
    const res = await fetch(`https://huggingface.co/api/datasets/${REPO}/tree?recursive=false`);
    if (!res.ok) throw new Error(`HF tree API failed: ${res.status}`);
    const items = await res.json();
    return items.filter((i) => i.type === 'folder').map((i) => i.path);
  } catch (err) {
    console.error(err);
    return [];
  }
}

// Views
window.renderHome = function () {
  if (!app) return;
  app.innerHTML = `
    <div class="card">
      <h1>Vanguard</h1>
      <p>Frontend router + dataset previews (HF CDN-bypass ready).</p>
      <p><a href="#/datasets" data-link>Browse datasets</a></p>
    </div>
  `;
  attachLinks();
};

window.renderDatasetList = async function () {
  if (!app) return;
  app.innerHTML = `<div class="card"><h2>Datasets</h2><p class="loading">Loading file list…</p></div>`;

  // Default folder; replace or make dynamic (e.g., pick latest)
  const dateFolder = 'batches/mirror-merged/2026-05-02';
  const files = await fetchFileList(dateFolder);

  if (files.length === 0) {
    app.innerHTML = `
      <div class="card">
        <h2>Datasets</h2>
        <p class="error">No files found in ${dateFolder}. Check repo/folder name.</p>
        <p><a href="#/" data-link>Home</a></p>
      </div>
    `;
    attachLinks();
    return;
  }

  app.innerHTML = `
    <div class="card">
      <h2>Datasets — ${dateFolder}</h2>
      <ul>
        ${files
          .map(
            (f) => `
          <li style="margin-bottom:0.5rem;">
            <a href="#/datasets/${encodeURIComponent(f.path)}" data-link>${f.path}</a>
            <br/>
            <small style="color:#64748b;">${f.cdn}</small>
          </li>
        `
          )
          .join('')}
      </ul>
      <p><a href="#/" data-link>← Home</a></p>
    </div>
  `;
  attachLinks();
};

window.renderDatasetDetail = async function (id) {
  const path = decodeURIComponent(id || '');
  if (!app) return;
  app.innerHTML = `<div class="card"><h2>Dataset</h2><p class="loading">Loading ${path}…</p></div>`;

  const cdn = `${API_ROOT}/${REPO}/resolve/main/${encodeURIComponent(path)}`;
  try {
    const res = await fetch(cdn);
    if (!res.ok) throw new Error(`Fetch failed: ${res.status}`);
    const text = await res.text();
    let preview = '';
    try {
      const parsed = JSON.parse(text);
      preview = `<pre style="white-space:pre-wrap;word-break:break-word;">${escapeHtml(
        JSON.stringify(parsed, null, 2)
      )}</pre>`;
    } catch {
      preview = `<pre style="white-space:pre-wrap;word-break:break-word;">${escapeHtml(
        text.slice(0, 2000)
      )}${text.length > 2000 ? '…' : ''}</pre>`;
    }

    app.innerHTML = `
      <div class="card">
        <h2>${path}</h2>
        <p><a href="${cdn}" target="_blank" rel="noopener">Open raw (CDN)</a></p>
        ${preview}
        <p><a href="#/datasets" data-link>← Back to list</a></p>
      </div>
    `;
  } catch (err) {
    app.innerHTML =
