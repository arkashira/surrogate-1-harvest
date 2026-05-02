# vanguard / quality

## 1. Diagnosis

- No deterministic scroll restoration on route changes (hash router) → disorienting jumps and poor UX on back/forward.
- Ephemeral UI state (filters/pagination/selection) not reflected in URL → reloads or shared links lose context.
- Missing canonical routes and query-string schema for dataset detail view (`#/datasets/:id`) → unshareable state.
- No loading/error UI during async transitions → perceived performance issues and blank states.
- Router lifecycle not integrated with scroll cache → browser default scroll reset not prevented.

## 2. Proposed change

File: `/opt/axentx/vanguard/src/router.js` (or create if absent)  
Scope: minimal hash router with scroll restoration + URL-synced state for dataset filters/pagination.  
Add:
- Deterministic scroll cache keyed by `location.key || pathname+search+hash`.
- On route change: prevent default scroll reset, restore cached position after render.
- Dataset detail route: reflect filter/pagination in query string (`?page=2&filter=open`).
- Lightweight loading state during async render.

## 3. Implementation

Create/update `/opt/axentx/vanguard/src/router.js`:

```js
// /opt/axentx/vanguard/src/router.js
// Minimal hash router with scroll restoration and URL-synced state
(function () {
  const ROUTE_DATA_ATTR = 'data-route';
  const SCROLL_CACHE = new Map();

  function getKey() {
    // location.key is ideal; fallback to full hash fragment
    if (location.hash && history.state && history.state.key) {
      return history.state.key;
    }
    return location.hash || '#/';
  }

  function saveScroll(key) {
    SCROLL_CACHE.set(key, { x: window.scrollX, y: window.scrollY });
  }

  function restoreScroll(key) {
    // restore after next paint; if no cache, scroll to top
    requestAnimationFrame(() => {
      if (SCROLL_CACHE.has(key)) {
        const { x, y } = SCROLL_CACHE.get(key);
        window.scrollTo(x, y);
      } else {
        window.scrollTo(0, 0);
      }
    });
  }

  function parseDatasetQuery() {
    const search = location.hash.split('?')[1] || '';
    const params = new URLSearchParams(search);
    return {
      page: Math.max(1, parseInt(params.get('page'), 10) || 1),
      filter: params.get('filter') || '',
    };
  }

  function pushDatasetState(id, { page, filter } = {}) {
    const query = new URLSearchParams();
    if (page && page > 1) query.set('page', String(page));
    if (filter) query.set('filter', filter);
    const q = query.toString() ? `?${query.toString()}` : '';
    const hash = `#/datasets/${id}${q}`;
    const key = hash + (history.state && history.state.key ? history.state.key : '');
    history.pushState({ key, ...history.state }, '', hash);
    route();
  }

  function showLoading(el) {
    if (!el) return;
    el.setAttribute(ROUTE_DATA_ATTR, 'loading');
  }

  function hideLoading(el) {
    if (!el) return;
    el.removeAttribute(ROUTE_DATA_ATTR);
  }

  function renderDatasetDetail(id, query) {
    const container = document.getElementById('app');
    if (!container) return;
    showLoading(container);

    // Simulate async fetch; replace with real data fetch
    return new Promise((resolve) => {
      setTimeout(() => {
        container.innerHTML = `
          <h1>Dataset ${id}</h1>
          <p>Filter: ${query.filter || 'none'}</p>
          <p>Page: ${query.page}</p>
          <button data-action="prev">Prev</button>
          <button data-action="next">Next</button>
          <input data-filter type="text" placeholder="filter" value="${query.filter || ''}">
          <button data-action="apply">Apply</button>
        `;
        hideLoading(container);
        restoreScroll(getKey());
        resolve();
      }, 200);
    });
  }

  function renderNotFound() {
    const container = document.getElementById('app');
    if (container) {
      container.innerHTML = '<h1>Not found</h1>';
    }
  }

  async function route() {
    const key = getKey();
    saveScroll(key); // save position before changing DOM

    const hash = location.hash.replace(/^#/, '');
    const [route, id] = hash.split('/');

    if (route === 'datasets' && id) {
      const query = parseDatasetQuery();
      await renderDatasetDetail(id, query);
      return;
    }

    if (hash === '' || hash === '/') {
      const container = document.getElementById('app');
      if (container) {
        container.innerHTML = '<h1>Home</h1><a href="#/datasets/123">Open dataset</a>';
        restoreScroll(getKey());
      }
      return;
    }

    renderNotFound();
    restoreScroll(getKey());
  }

  function onHashChange() {
    route();
  }

  function onPopState() {
    route();
  }

  // Delegate clicks for in-app navigation to avoid full reloads
  function onDocumentClick(e) {
    const anchor = e.target.closest('a[href^="#"]');
    if (!anchor) return;
    const href = anchor.getAttribute('href');
    if (href === '#') return;
    e.preventDefault();
    const key = href + (Math.random().toString(36).slice(2));
    history.pushState({ key }, '', href);
    route();
  }

  function onActionClick(e) {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const action = btn.getAttribute('data-action');
    const container = document.getElementById('app');
    if (!container) return;

    const currentId = (location.hash.match(/#\/datasets\/([^?]+)/) || [])[1];
    if (!currentId) return;

    const query = parseDatasetQuery();
    if (action === 'prev') {
      pushDatasetState(currentId, { ...query, page: Math.max(1, query.page - 1) });
    } else if (action === 'next') {
      pushDatasetState(currentId, { ...query, page: query.page + 1 });
    } else if (action === 'apply') {
      const input = container.querySelector('[data-filter]');
      pushDatasetState(currentId, { ...query, filter: input ? input.value : '', page: 1 });
    }
  }

  // Init
  function init() {
    // Prevent browser default scroll reset on hash change where possible
    if ('scrollRestoration' in history) {
      history.scrollRestoration = 'manual';
    }

    window.addEventListener('hashchange', onHashChange);
    window.addEventListener('popstate', onPopState);
    document.addEventListener('click', onDocumentClick);
    document.addEventListener('click', onActionClick);

    // Initial route
    route();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
```

If no `src/` exists, create it and add a minimal HTML entry at `/opt/axentx/vanguard/index.html`:

```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Vanguard</title>
    <style>
      [data-route="loading"] { opacity: 0.6; pointer-events: none; }
      [data-route="loading"]::after { content: "Loading..."; display:inline-block; margin-left:8px; }
    </style>
  </head>
  <body>
    <div id="app"></div>
    <script src="./src/router.js"></script>
  </body>
</html>
```

## 4. Verification

1. Serve the project (e.g., `python3 -m http.server 8000` from `/opt/axentx/vanguard`).
2. Open `http://localhost:8000` and
