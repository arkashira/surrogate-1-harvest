# vanguard / quality

## 1. Diagnosis

- Hash router exists but scroll position is **not restored** on back/forward navigation → disorienting jumps to top on every route change.
- Dataset detail view (`#/datasets/:id`) and list filters/pagination are **not persisted in URL** → reloads or shared links lose context.
- No loading or error UI states during dataset fetch/router transitions → blank screens or silent failures.
- Route transitions do not **scroll to target element** (e.g., dataset card or top of detail) when navigating via link or browser history.
- Missing canonical routes for common entry points (e.g., `/datasets`, `/datasets/:id`) → poor shareability and SEO.

## 2. Proposed change

**Scope**: `src/router.js` (or equivalent) + `src/views/DatasetListView.js` + `src/views/DatasetDetailView.js`  
**Goal**: Add scroll restoration + URL-persisted filters/pagination + loading/error UI in <2h.

- Add `scrollRestoration` logic tied to hash router (store/restore scroll positions per route key).
- Encode dataset list state (`page`, `pageSize`, `filters`) in hash query params (e.g., `#/datasets?page=2&size=20&filter=...`).
- Parse query params on load and apply to list view; update URL on filter/pagination changes.
- Add lightweight loading/error placeholders in views during async dataset fetch.
- On route change, scroll to top of content area (or to dataset card if navigating to detail).

## 3. Implementation

```js
// src/router.js
(function () {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // Parse hash into { route, query }
  function parseHash(hash = location.hash) {
    if (!hash || hash === '#') return { route: '/', query: {} };
    const [path, search] = hash.replace(/^#/, '').split('?');
    const query = {};
    if (search) {
      for (const pair of search.split('&')) {
        const [k, v] = pair.split('=').map(decodeURIComponent);
        if (k) query[k] = v === undefined ? '' : v;
      }
    }
    return { route: path || '/', query };
  }

  // Serialize query object
  function stringifyQuery(q) {
    const parts = [];
    for (const k of Object.keys(q).sort()) {
      const v = q[k];
      if (v === undefined || v === null || v === '') continue;
      parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
    }
    return parts.length ? '?' + parts.join('&') : '';
  }

  // Build hash
  function buildHash(route, query) {
    return '#' + route.replace(/^\//, '') + stringifyQuery(query);
  }

  // Scroll restoration map: routeKey -> { x, y }
  const scrollCache = new Map();
  const STORAGE_KEY = 'vanguard-scroll-cache';
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      Object.entries(parsed || {}).forEach(([k, v]) => scrollCache.set(k, v));
    }
  } catch (e) {
    // ignore
  }

  function saveScrollCache(key, x = window.scrollX, y = window.scrollY) {
    scrollCache.set(key, { x, y });
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(scrollCache)));
    } catch (e) {
      // ignore
    }
  }

  function restoreScroll(key) {
    const pos = scrollCache.get(key);
    if (pos) {
      window.scrollTo(pos.x, pos.y);
    } else {
      // default: scroll to top of content
      const content = $('#content') || $('main') || document.body;
      if (content) content.scrollIntoView({ behavior: 'auto' });
      window.scrollTo(0, 0);
    }
  }

  // Loading / error UI helpers
  function showLoading(container) {
    if (!container) return;
    container.innerHTML = '<div class="loading">Loading…</div>';
  }

  function showError(container, message) {
    if (!container) return;
    container.innerHTML = `<div class="error">Error: ${message}</div>`;
  }

  // Route handlers registry
  const routes = new Map();

  function route(path, handler) {
    routes.set(path, handler);
  }

  // Current active controller (for aborting)
  let currentAbort = null;

  async function navigate(hash = location.hash, push = false) {
    if (currentAbort) {
      currentAbort.abort();
      currentAbort = null;
    }
    const controller = new AbortController();
    currentAbort = controller;

    const { route: r, query } = parseHash(hash);
    const routeKey = r + stringifyQuery(query);

    // Persist scroll for previous route before leaving
    const prevKey = window.__currentRouteKey;
    if (prevKey) saveScrollCache(prevKey);

    // Update URL
    if (push) history.pushState(null, '', hash);
    else if (location.hash !== hash) location.replace(hash);

    window.__currentRouteKey = routeKey;

    const handler = routes.get(r) || routes.get('/404') || (() => {});
    const container = $('#content') || $('main') || document.body;

    showLoading(container);
    try {
      await handler({ query, container, signal: controller.signal, routeKey });
      restoreScroll(routeKey);
    } catch (err) {
      if (err.name !== 'AbortError') {
        console.error(err);
        showError(container, err.message || 'Failed to load');
      }
    } finally {
      if (currentAbort === controller) currentAbort = null;
    }
  }

  // Public API
  window.Router = {
    route,
    navigate: (path, query = {}, push = true) => {
      const hash = buildHash(path, query);
      navigate(hash, push);
    },
    go: (delta) => history.go(delta),
    parseHash,
    buildHash
  };

  // Hash change & popstate
  window.addEventListener('hashchange', () => navigate(location.hash, true));
  window.addEventListener('popstate', () => navigate(location.hash, false));

  // Initial load
  if (!location.hash) {
    location.replace('#/');
  } else {
    navigate(location.hash, false);
  }
})();
```

```js
// src/views/DatasetListView.js
(function () {
  const ITEMS_PER_PAGE = 20;

  function renderList(container, datasets, query) {
    const page = Math.max(1, parseInt(query.page || '1', 10));
    const size = Math.max(1, parseInt(query.size || String(ITEMS_PER_PAGE), 10));
    const filter = (query.filter || '').trim().toLowerCase();

    const filtered = filter
      ? datasets.filter((d) => (d.name || '').toLowerCase().includes(filter) || (d.description || '').toLowerCase().includes(filter))
      : datasets;

    const total = filtered.length;
    const totalPages = Math.max(1, Math.ceil(total / size));
    const paged = filtered.slice((page - 1) * size, page * size);

    container.innerHTML = `
      <div class="dataset-list-header">
        <input class="filter-input" type="search" placeholder="Filter datasets…" value="${filter ? escapeHtml(filter) : ''}">
      </div>
      <div class="dataset-list">
        ${paged.length ? paged.map((d) => datasetCard(d)).join('') : '<div class="empty">No datasets found</div>'}
      </div>
      <div class="pagination">
        <button class="prev" ${page <= 1 ? 'disabled' : ''}>Prev</button>
        <span class="page-info">Page ${page} of ${totalPages} (${total} total)</span>
        <button class="next" ${page >= totalPages ? 'disabled' : ''}>Next</button>
      </div>
    `;

    // Wire controls
    container.querySelector('.filter-input').addEventListener('input', (e) => {
      Router
