# vanguard / quality

## Final consolidated implementation

Create `/opt/axentx/vanguard/src/router.js`:

```js
// src/router.js
// URL-driven dataset selection, filters, pagination, and robust scroll handling.
// - Hash routes: #/  and  #/datasets/:id
// - Query params: filter, page, pageSize, sort, dataset (selection)
// - Scroll: per-route saved/restored, sticky header compensation,
//   list scroll anchoring on filter/page/sort changes, detail scroll-into-view.

(function () {
  'use strict';

  // -------------------------
  // Helpers
  // -------------------------
  function parseHash() {
    const raw = location.hash || '#/';
    const [path, search] = raw.replace(/^#/, '').split('?');
    const params = new URLSearchParams(search || '');
    return { path: path || '/', params, raw };
  }

  function buildHash(path, params = {}) {
    const sp = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v == null || v === '') return;
      sp.set(k, String(v));
    });
    const search = sp.toString();
    return '#' + path + (search ? '?' + search : '');
  }

  // -------------------------
  // Config
  // -------------------------
  const routes = [
    { path: '/', handler: onRoot },
    { path: '/datasets/:id', handler: onDatasetDetail }
  ];

  const HEADER_HEIGHT = 64; // px — adjust to your fixed header height
  const SCROLL_KEY_PREFIX = 'vanguard_scroll_';
  const LIST_ANCHOR_KEY = 'vanguard_list_anchor'; // dataset id last scrolled into view

  let current = parseHash();
  let listeners = [];
  let isRestoringScroll = false;

  // -------------------------
  // App contract (safe defaults)
  // -------------------------
  function getApp() {
    return window.__vanguardApp__ || null;
  }

  // -------------------------
  // Scroll utilities
  // -------------------------
  function saveScroll() {
    try {
      sessionStorage.setItem(SCROLL_KEY_PREFIX + current.path, String(window.scrollY));
    } catch (e) {
      // ignore
    }
  }

  function restoreScroll() {
    if (isRestoringScroll) return;
    isRestoringScroll = true;
    try {
      const y = sessionStorage.getItem(SCROLL_KEY_PREFIX + current.path);
      if (y != null) {
        window.scrollTo(0, parseInt(y, 10));
      } else {
        window.scrollTo(0, 0);
      }
    } catch (e) {
      window.scrollTo(0, 0);
    } finally {
      // allow normal scroll saves again shortly
      setTimeout(() => { isRestoringScroll = false; }, 150);
    }
  }

  function saveListAnchor(datasetId) {
    try {
      sessionStorage.setItem(LIST_ANCHOR_KEY, datasetId || '');
    } catch (e) {
      // ignore
    }
  }

  function restoreListAnchor() {
    try {
      return sessionStorage.getItem(LIST_ANCHOR_KEY) || null;
    } catch (e) {
      return null;
    }
  }

  function scrollToDatasetItem(id, options = {}) {
    const el = document.getElementById('dataset-' + id);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start', ...options });
      saveListAnchor(id);
      return true;
    }
    return false;
  }

  // Apply scroll-padding for fixed header when using fragment links
  function applyScrollPadding() {
    try {
      document.documentElement.style.scrollPaddingTop = HEADER_HEIGHT + 'px';
    } catch (e) {
      // ignore
    }
  }

  // -------------------------
  // Query/state sync
  // -------------------------
  function applyQueryState(params, opts = {}) {
    const app = getApp();
    if (!app) return;

    const silent = Boolean(opts.silent);

    if (params.has('filter') && app.setFilter) {
      app.setFilter(params.get('filter'), { silent });
    }
    if (params.has('page') && app.setPage) {
      app.setPage(parseInt(params.get('page'), 10) || 1, { silent });
    }
    if (params.has('pageSize') && app.setPageSize) {
      app.setPageSize(parseInt(params.get('pageSize'), 10) || 20, { silent });
    }
    if (params.has('sort') && app.setSort) {
      app.setSort(params.get('sort'), { silent });
    }
    // dataset selection is handled by route handlers
  }

  function captureQueryState() {
    const app = getApp();
    const out = {};
    if (!app) return out;

    if (app.getFilter) out.filter = app.getFilter();
    if (app.getPage) out.page = app.getPage();
    if (app.getPageSize) out.pageSize = app.getPageSize();
    if (app.getSort) out.sort = app.getSort();
    return out;
  }

  // -------------------------
  // Route handlers
  // -------------------------
  function onRoot() {
    const app = getApp();
    if (app && app.selectDataset) app.selectDataset(null);
    applyQueryState(current.params);

    // If returning from detail, try to restore list position to last viewed item
    const anchorId = restoreListAnchor();
    if (anchorId) {
      // attempt scroll; if item not rendered, rely on saved scroll position
      if (!scrollToDatasetItem(anchorId)) {
        restoreScroll();
      }
    } else {
      restoreScroll();
    }
  }

  function onDatasetDetail(matches) {
    const id = matches.id;
    const app = getApp();
    if (app && app.selectDataset) app.selectDataset(id);
    applyQueryState(current.params);

    // Scroll to detail container (or top)
    requestAnimationFrame(() => {
      const el = document.getElementById('dataset-detail');
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } else {
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }
    });
  }

  // -------------------------
  // Matching & dispatch
  // -------------------------
  function matchRoute(pathname) {
    for (const r of routes) {
      const pattern = r.path.replace(/:\w+/g, '([^/]+)');
      const re = new RegExp('^' + pattern + '$');
      const m = pathname.match(re);
      if (m) {
        const keys = (r.path.match(/:\w+/g) || []).map((k) => k.slice(1));
        const matches = { _: r.path };
        keys.forEach((k, i) => (matches[k] = decodeURIComponent(m[i + 1])));
        return { handler: r.handler, matches };
      }
    }
    return null;
  }

  function route() {
    current = parseHash();
    const matched = matchRoute(current.path);
    if (matched) {
      matched.handler(matched.matches);
    } else {
      // fallback to home
      navigate('/', {}, true);
    }
    listeners.forEach((fn) => fn(current));
  }

  // -------------------------
  // Navigation API
  // -------------------------
  function navigate(path, params = {}, replace = false) {
    const href = buildHash(path, params);
    if (replace) {
      history.replaceState(null, '', href);
    } else {
      history.pushState(null, '', href);
    }
    route();
  }

  function goToHome() {
    // preserve query state when going home (filter/page/etc)
    navigate('/', captureQueryState());
  }

  function goToDataset(id, params = {}) {
    const merged = { ...captureQueryState(), ...params };
    navigate('/datasets/' + encodeURIComponent(id), merged);
  }

  function updateQuery(params = {}, replace = true) {
    const merged = { ...Object.fromEntries(current.params), ...params };
    const href = buildHash(current.path, merged);
    if (replace) {
      history.replace
