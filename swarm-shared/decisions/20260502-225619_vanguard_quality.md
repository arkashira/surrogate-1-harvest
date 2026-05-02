# vanguard / quality

## Final synthesized implementation

### Diagnosis (merged)
- Hash router lacks scroll restoration on back/forward → disorienting jumps.
- Dataset detail view (`#/datasets/:id`) does not persist filter/pagination in URL → reloads/shared links lose context.
- No loading/error UI for async dataset fetch → blank screens/silent failures.
- Missing active route link styles and `aria-current` → accessibility/UX clarity issues.
- No scroll-to-top on forward navigation to a new route.
- No 404/fallback route for unknown hashes → silent no-ops.
- URLs can have inconsistent trailing slashes/duplicate query params.

### Files to create/modify
- `/opt/axentx/vanguard/index.html` — add loading/error UI and active-link styles.
- `/opt/axentx/vanguard/src/router.js` — robust router with scroll restoration, URL-state sync, loading/error boundaries, 404, and normalization.
- Include router in `index.html`.

---

### `index.html` (add inside `<body>`)
```html
<!-- Loading overlay -->
<div id="app-overlay" aria-hidden="true" style="display:none;position:fixed;inset:0;background:rgba(255,255,255,0.75);z-index:9999;align-items:center;justify-content:center;">
  <div role="status" style="padding:1rem;background:#fff;border:1px solid #e2e8f0;border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,0.08);display:flex;align-items:center;gap:0.75rem;">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="10" stroke-opacity="0.2"/>
      <path d="M12 2a10 10 0 0 1 10 10"/>
    </svg>
    <span>Loading…</span>
  </div>
</div>

<!-- Error banner -->
<div id="app-error" role="alert" aria-live="assertive" style="display:none;margin:1rem;padding:1rem;border-radius:8px;border:1px solid #f5c6cb;background:#f8d7da;color:#721c24;display:flex;align-items:center;justify-content:space-between;gap:1rem;">
  <div style="display:flex;align-items:center;gap:0.5rem;">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
    <span id="app-error-message">Failed to load dataset.</span>
  </div>
  <button id="app-error-retry" type="button" style="padding:0.35rem 0.75rem;border-radius:6px;border:1px solid rgba(0,0,0,0.08);background:#fff;cursor:pointer;">Retry</button>
</div>

<!-- 404 fallback (hidden by default) -->
<div id="app-not-found" role="status" aria-live="polite" style="display:none;padding:2rem;text-align:center;color:#4a5568;">
  <h2 style="margin:0 0 0.5rem;font-size:1.25rem;">Page not found</h2>
  <p style="margin:0 0 1rem;font-size:0.95rem;">The requested dataset or route could not be found.</p>
  <a href="#/datasets" data-router-link style="color:#3182ce;text-decoration:underline;">Go to datasets</a>
</div>

<style>
  .nav-link[aria-current="page"] { font-weight:700; text-decoration:underline; }
  .nav-link.active { font-weight:700; text-decoration:underline; }
</style>
```

---

### `/opt/axentx/vanguard/src/router.js`
```javascript
(function () {
  'use strict';

  // Utility
  function qs() { try { return new URLSearchParams(location.hash.split('?')[1] || ''); } catch { return new URLSearchParams(); } }
  function normalizeHashPath(path) {
    if (!path) return '/';
    // Remove duplicate slashes, trailing slash (except root), and normalize
    return '/' + path.replace(/^\/+|\/+$/g, '').replace(/\/+/g, '/');
  }
  function normalizeHash() {
    const raw = location.hash.replace(/^#/, '');
    const [pathPart, searchPart] = raw.split('?');
    const normPath = normalizeHashPath(pathPart);
    const params = new URLSearchParams(searchPart || '');
    // Deduplicate and sort params for consistency
    const seen = new Set();
    const normalized = new URLSearchParams();
    for (const [k, v] of params) {
      if (seen.has(k)) continue;
      seen.add(k);
      if (v !== undefined && v !== null && v !== '') normalized.set(k, v);
    }
    const search = normalized.toString() ? '?' + normalized.toString() : '';
    const target = '#' + normPath + search;
    if (location.hash !== target) history.replaceState(null, '', target);
    return { path: normPath, params: normalized };
  }

  // State
  const state = {
    datasets: [],
    filters: { search: '', status: '' },
    pagination: { page: 1, perPage: 20 },
    current: null,
    loading: false,
    error: null
  };

  const $ = (sel) => document.querySelector(sel);
  const showOverlay = (show) => { const el = $('#app-overlay'); if (el) el.style.display = show ? 'flex' : 'none'; };
  const showError = (msg, show) => {
    const el = $('#app-error');
    if (!el) return;
    el.style.display = show ? 'flex' : 'none';
    const msgEl = $('#app-error-message');
    if (msgEl) msgEl.textContent = msg || 'Failed to load dataset.';
  };
  const showNotFound = (show) => {
    const el = $('#app-not-found');
    if (el) el.style.display = show ? 'block' : 'none';
  };

  function parseRoute() {
    const { path, params } = normalizeHash();
    const segments = path.split('/').filter(Boolean);
    return { path, params, segments };
  }

  function syncStateFromURL() {
    const { params, segments } = parseRoute();
    state.filters.search = params.get('q') || '';
    state.filters.status = params.get('status') || '';
    state.pagination.page = Math.max(1, parseInt(params.get('page'), 10) || 1);
    state.pagination.perPage = Math.max(1, parseInt(params.get('perPage'), 10) || 20);
    state.current = segments[0] === 'datasets' && segments[1] ? segments[1] : null;
    return { segments };
  }

  function buildHashForDatasetsList() {
    const sp = new URLSearchParams();
    if (state.filters.search) sp.set('q', state.filters.search);
    if (state.filters.status) sp.set('status', state.filters.status);
    if (state.pagination.page > 1) sp.set('page', String(state.pagination.page));
    if (state.pagination.perPage !== 20) sp.set('perPage', String(state.pagination.perPage));
    return '#/datasets' + (sp.toString() ? '?' + sp.toString() : '');
  }

  function updateURLFromState(replace) {
    const hash = state.current ? `#/datasets/${state.current}` : buildHashForDatasetsList();
    if (location.hash !==
