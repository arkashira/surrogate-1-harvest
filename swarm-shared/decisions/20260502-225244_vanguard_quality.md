# vanguard / quality

## Final Answer (Unified, Correct, Actionable)

**Scope:** Add a minimal, framework-agnostic hash router that synchronizes URL ↔ UI state, enables deep links (`#/`, `#/datasets`, `#/datasets/:id`), preserves filters/pagination in query params, restores scroll on back/forward, and intercepts internal links to avoid full-page reloads.

---

### 1. Files to Create / Modify
- **Create:** `src/router.js` (router + state serialization)
- **Modify:** `src/main.js` (mount router, App bridge)
- **Update pages:** `DatasetList` and `DatasetDetail` modules to accept state from router and push UI changes back to URL (replace=true) when filters/page/sort change.

---

### 2. `src/router.js`
```js
// Lightweight hash router with URL-synchronized state.
// Routes:
//   #/                      -> home
//   #/datasets              -> dataset list
//   #/datasets/:id          -> dataset detail
// Query params: filter, page, pageSize, sort (preserved/merged)

(function (global) {
  'use strict';

  const routes = [
    { path: '/', name: 'home' },
    { path: '/datasets', name: 'datasets' },
    { path: '/datasets/:id', name: 'dataset-detail' }
  ];

  function normalizeHash() {
    const raw = location.hash || '#/';
    return raw.replace(/^#/, '');
  }

  function parsePathname(pathname) {
    for (const r of routes) {
      const keys = r.path.match(/:([^\/]+)/g) || [];
      const pattern = '^' + r.path.replace(/:[^\/]+/g, '([^\\/]+)') + '$';
      const re = new RegExp(pattern);
      const m = pathname.match(re);
      if (m) {
        const params = {};
        keys.forEach((k, i) => { params[k.slice(1)] = decodeURIComponent(m[i + 1]); });
        return { route: r, params };
      }
    }
    return { route: null, params: {} };
  }

  function parseHash() {
    const full = normalizeHash();
    const [pathname, search] = full.split('?');
    const { route, params } = parsePathname(pathname);
    const query = Object.fromEntries(new URLSearchParams(search || ''));
    return { route, params, query, pathname };
  }

  function parseState() {
    const { query } = parseHash();
    return {
      filter: query.filter || '',
      page: Math.max(1, parseInt(query.page, 10) || 1),
      pageSize: Math.max(1, parseInt(query.pageSize, 10) || 20),
      sort: query.sort || ''
    };
  }

  function buildHash({ routeName, params, query } = {}) {
    let path = '/';
    if (routeName === 'datasets') path = '/datasets';
    if (routeName === 'dataset-detail' && params?.id) path = `/datasets/${encodeURIComponent(params.id)}`;

    const q = new URLSearchParams(query || {}).toString();
    return '#' + path + (q ? '?' + q : '');
  }

  function navigate(to, { replace = false } = {}) {
    const next = (typeof to === 'string') ? to : buildHash(to);
    const target = location.pathname + location.search + next;
    if (replace) {
      location.replace(target);
    } else {
      // Avoid adding duplicate entries when already at target
      if (normalizeHash() !== next.replace(/^#/, '')) {
        location.hash = next.slice(1);
      } else {
        // Still trigger route to ensure state sync
        route();
      }
    }
  }

  let activeRouteName = null;

  function route() {
    const { route, params, query } = parseHash();
    activeRouteName = route ? route.name : 'home';

    const state = {
      routeName: activeRouteName,
      params,
      query,
      ...parseState()
    };

    // Delegate to App bridge
    if (global.App && typeof global.App.navigateTo === 'function') {
      global.App.navigateTo(activeRouteName, state);
    }

    updateActiveLinks();
    return state;
  }

  function updateActiveLinks() {
    const { pathname } = parseHash();
    document.querySelectorAll('[data-route]').forEach((el) => {
      const href = el.getAttribute('href') || el.getAttribute('data-route');
      if (!href) return;
      const targetPath = href.replace(/^#/, '');
      const isActive = targetPath === '/' ? pathname === '/' : pathname.startsWith(targetPath + '/') || targetPath === pathname;
      el.classList.toggle('active', isActive);
    });
  }

  function interceptClicks() {
    document.addEventListener('click', (e) => {
      const anchor = e.target.closest('a[data-route], a[href^="#"]');
      if (!anchor) return;

      const href = anchor.getAttribute('href');
      // Only handle hash-based internal routes
      if (href && href.startsWith('#') && !href.startsWith('#http')) {
        e.preventDefault();
        navigate(href);
      }
    });
  }

  // Scroll restoration: save on change, restore on hashchange
  const scrollState = {};

  function saveScroll() {
    const key = parseHash().pathname || '/';
    scrollState[key] = window.scrollY || window.pageYOffset;
  }

  function restoreScroll() {
    const key = parseHash().pathname || '/';
    const y = scrollState[key];
    if (typeof y === 'number') {
      window.scrollTo(0, y);
    } else {
      // For detail pages, scroll to top; for lists, preserve sensible default
      window.scrollTo(0, 0);
    }
  }

  // Public API
  const Router = {
    init() {
      window.addEventListener('hashchange', () => {
        saveScroll();
        route();
        // restore after route + render (next frame)
        requestAnimationFrame(() => restoreScroll());
      });
      interceptClicks();
      // initial
      route();
      restoreScroll();
    },
    navigate,
    buildHash,
    parseHash,
    parseState,
    gotoHome: () => navigate('#/'),
    gotoDatasets: (query) => navigate(buildHash({ routeName: 'datasets', query })),
    gotoDataset: (id, query) => navigate(buildHash({ routeName: 'dataset-detail', params: { id }, query }))
  };

  global.Router = Router;
})(window);
```

---

### 3. `src/main.js`
```js
import { Router } from './router.js';

// Minimal App bridge: pages should register handlers or expose functions.
window.App = {
  navigateTo(page, state = {}) {
    // Hide all pages first (adjust selectors to match your markup)
    document.querySelectorAll('.page').forEach((p) => { p.hidden = true; });

    if (page === 'home') {
      const el = document.getElementById('page-home');
      if (el) el.hidden = false;
    } else if (page === 'datasets') {
      const el = document.getElementById('page-datasets');
      if (el) el.hidden = false;
      if (window.DatasetList && typeof window.DatasetList.applyState === 'function') {
        window.DatasetList.applyState(state);
      }
    } else if (page === 'dataset-detail') {
      const el = document.getElementById('page-dataset-detail');
      if (el) el.hidden = false;
      if (window.DatasetDetail && typeof window.DatasetDetail.render === 'function') {
        window.DatasetDetail.render(state);
      }
    }

    // Persist UI state changes back to URL (replace) when filters/page/sort change.
    // This keeps URL in sync without creating extra history entries on every keystroke.
    if (state && (state.filter || state.page > 1 || state.sort || state.pageSize !== 20)) {
      const current = Router.parseHash();
      const mergedQuery = {
        ...current.query,
        ...(state.filter ? { filter: state.filter }
