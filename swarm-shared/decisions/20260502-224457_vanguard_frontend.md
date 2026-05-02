# vanguard / frontend

## 1. Diagnosis

- No client-side router or URL-driven state; navigation is likely full-page reloads or manual DOM swaps, causing poor UX and lost state on refresh.
- Missing dataset detail view (`#/datasets/:id`) prevents deep-linking and sharing specific dataset pages.
- No hash-based routing means browser history/back-button support is absent or unreliable.
- No lightweight route-to-component mapping; UI logic is likely tangled in imperative code.
- Missing loading/error states for dataset navigation creates janky transitions and unclear failure modes.

## 2. Proposed change

Add a minimal, dependency-free hash router and two views (list + detail) to `vanguard/frontend/` (or equivalent). Scope:

- Create `frontend/router.js` — ~60-line hash router with `navigate`, `onRoute`, and param extraction.
- Create `frontend/views/DatasetListView.js` — renders dataset list and links with `#/datasets/:id`.
- Create `frontend/views/DatasetDetailView.js` — fetches and renders a single dataset by id (CDN-bypass friendly: uses pre-listed `file-list.json` or slug).
- Update `index.html` to mount into a root element and load router + views.
- Add CSS classes for loading/error states in `styles.css` (or inline minimal styles).

## 3. Implementation

Below are concrete files to add/modify. Adjust paths if your frontend layout differs.

### frontend/router.js
```js
// Minimal hash router for vanguard frontend
(function (global) {
  const routes = [];

  function parseParams(pattern, path) {
    const keys = [];
    const re = pattern.replace(/:\w+/g, (m) => {
      keys.push(m.slice(1));
      return '([^/]+)';
    }).replace(/\//g, '\\/') + '(?:\\?.*)?$';
    const matches = path.match(new RegExp('^' + re));
    if (!matches) return null;
    const params = {};
    keys.forEach((k, i) => { params[k] = decodeURIComponent(matches[i + 1]); });
    return params;
  }

  function hashPath() {
    const hash = location.hash || '#/';
    return hash.replace(/^#/, '') || '/';
  }

  function on(pattern, handler) {
    routes.push({ pattern, handler });
  }

  function navigate(path, replace = false) {
    const href = '#' + path;
    if (replace) {
      location.replace(location.pathname + location.search + href);
    } else {
      location.hash = path;
    }
  }

  function run() {
    const path = hashPath();
    for (const r of routes) {
      const params = parseParams(r.pattern, path);
      if (params !== null) {
        return r.handler(params, path);
      }
    }
    // fallback to / if no match
    if (path !== '/') navigate('/', true);
  }

  global.Router = { on, navigate, run };

  window.addEventListener('hashchange', run);
  document.addEventListener('DOMContentLoaded', run);
})(window);
```

### frontend/views/DatasetListView.js
```js
// Renders dataset list and links to detail view
(function (global) {
  const API_ROOT = '/api'; // adjust if needed
  const CDN_ROOT = 'https://huggingface.co/datasets'; // for CDN-bypass links

  function renderList(datasets) {
    const container = document.getElementById('app');
    if (!container) return;
    container.innerHTML = `
      <h1>Datasets</h1>
      <div class="dataset-list">
        ${datasets.map(d => `
          <div class="dataset-card">
            <h3><a href="#/datasets/${encodeURIComponent(d.id)}">${escapeHtml(d.name || d.id)}</a></h3>
            <p class="meta">${escapeHtml(d.description || '')}</p>
            <p><small><a href="${CDN_ROOT}/${encodeURIComponent(d.repo || d.id)}" target="_blank" rel="noopener">HF repo</a></small></p>
          </div>
        `).join('')}
      </div>
    `;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (m) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[m];
    });
  }

  async function fetchDatasets() {
    // Prefer pre-computed file-list or catalog endpoint; fallback to /api/datasets
    try {
      const res = await fetch(`${API_ROOT}/datasets`);
      if (!res.ok) throw new Error('Failed to load datasets');
      return await res.json();
    } catch (err) {
      console.error(err);
      return [];
    }
  }

  function mount() {
    const container = document.getElementById('app');
    if (!container) return;
    container.innerHTML = '<div class="loading">Loading datasets...</div>';
    fetchDatasets().then(datasets => {
      renderList(datasets.length ? datasets : []);
    }).catch(() => {
      container.innerHTML = '<div class="error">Could not load datasets. Try again later.</div>';
    });
  }

  global.DatasetListView = { mount };
})(window);
```

### frontend/views/DatasetDetailView.js
```js
// Renders a single dataset detail; uses CDN-bypass file list when available
(function (global) {
  const CDN_ROOT = 'https://huggingface.co/datasets';

  function renderDetail(dataset, fileList) {
    const container = document.getElementById('app');
    if (!container) return;
    const filesHtml = fileList && fileList.length
      ? `<ul class="file-list">${fileList.map(f => `<li><a href="${CDN_ROOT}/${encodeURIComponent(dataset.repo || dataset.id)}/resolve/main/${encodeURIComponent(f)}" target="_blank" rel="noopener">${escapeHtml(f)}</a></li>`).join('')}</ul>`
      : '<p class="muted">No file list available.</p>';

    container.innerHTML = `
      <p><a href="#/datasets">&larr; Back to list</a></p>
      <h1>${escapeHtml(dataset.name || dataset.id)}</h1>
      <p class="meta">${escapeHtml(dataset.description || '')}</p>
      <p><a href="${CDN_ROOT}/${encodeURIComponent(dataset.repo || dataset.id)}" target="_blank" rel="noopener">HF repo</a></p>
      <h2>Files (CDN)</h2>
      ${filesHtml}
    `;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (m) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[m];
    });
  }

  async function fetchDataset(id) {
    try {
      const res = await fetch(`/api/datasets/${encodeURIComponent(id)}`);
      if (!res.ok) throw new Error('Dataset not found');
      return await res.json();
    } catch (err) {
      console.error(err);
      return null;
    }
  }

  async function fetchFileList(repoOrId) {
    // Try CDN-bypass file-list.json first (pre-listed by ops)
    try {
      const res = await fetch(`${CDN_ROOT}/${encodeURIComponent(repoOrId)}/resolve/main/file-list.json`);
      if (res.ok) return await res.json();
    } catch (e) {
      // ignore
    }
    return null;
  }

  async function mount(params) {
    const id = params && params.id;
    const container = document.getElementById('app');
    if (!container) return;
    if (!id) {
      container.innerHTML = '<div class="error">Missing dataset id.</div>';
      return;
    }
    container.innerHTML = '<div class="loading">Loading dataset...</div>';
    const dataset = await fetchDataset(id);
    if (!dataset) {
      container.innerHTML = '<div class="error">Dataset not found.</div>';
      return;
    }
    const fileList = await fetchFileList
