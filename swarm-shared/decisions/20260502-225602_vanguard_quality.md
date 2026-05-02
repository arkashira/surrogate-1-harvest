# vanguard / quality

## Final consolidated implementation

**Diagnosis (resolved)**  
- No router → full reloads, broken deep links, unshareable state.  
- Ephemeral UI state (filters/pagination/selection) lost on reload/navigation.  
- Scroll resets on route change.  
- No canonical routes or query-param sync → non-reproducible links.  

**Chosen approach**  
- Minimal, dependency-free hash router with query-param synchronization, scroll restoration, and idempotent render wiring.  
- Keep state in URL (hash path + query) as the source of truth; keep lightweight runtime cache to avoid re-fetch and to support fast back/forward.  
- Preserve scroll per-route with session storage; restore after paint; scroll-to-top only on cross-route navigation.  
- Guard against double-init and broken DOM.

---

### 1) Router (`/opt/axentx/vanguard/frontend/router.js`)
```js
// Minimal hash router: path matching, query sync, scroll restore, idempotent init
(function () {
  const routes = [];
  let notFound = () => {};
  let currentPath = null;
  let isInitialized = false;

  function parseHash() {
    const raw = (location.hash || '#/').slice(1);
    const [pathWithQuery = '/', ...searchParts] = raw.split('?');
    const path = pathWithQuery || '/';
    const search = searchParts.length ? '?' + searchParts.join('?') : '';
    const params = new URLSearchParams(search);
    return { path, search, params, hash: location.hash };
  }

  function normalizePathname(path) {
    return (path || '/').replace(/\/+$/, '') || '/';
  }

  function compile(path) {
    const keys = [];
    const regexString = path
      .replace(/([.+*?^${}()|[\]\\])/g, '\\$1')
      .replace(/\\:([a-zA-Z0-9_]+)/g, (_, key) => {
        keys.push(key);
        return '([^/]+)';
      })
      .replace(/\\\*/g, '(.*)');
    return { regex: new RegExp('^' + regexString + '$'), keys };
  }

  function match(pathname) {
    const normalized = normalizePathname(pathname);
    for (const r of routes) {
      const m = r.regex.exec(normalized);
      if (m) {
        const params = {};
        r.keys.forEach((k, i) => (params[k] = m[i + 1]));
        return { handler: r.handler, params, search: parseHash().search };
      }
    }
    return null;
  }

  function router(path, handler) {
    const { regex, keys } = compile(path);
    routes.push({ path, regex, keys, handler });
    return router;
  }

  router.on = router;
  router.notFound = (handler) => {
    notFound = handler;
    return router;
  };

  function render() {
    const { path, params } = parseHash();
    const pathname = normalizePathname(path);
    const matched = match(pathname);
    currentPath = pathname;
    if (matched) {
      matched.handler({
        params: matched.params,
        query: params,
        path: pathname,
        hash: location.hash,
      });
    } else {
      notFound({ params: {}, query: params, path: pathname, hash: location.hash });
    }
  }

  function navigate(to, replace = false) {
    const target = to.startsWith('#') ? to : '#' + (to.startsWith('/') ? to : '/' + to);
    const dest = location.pathname + location.search + target;
    if (replace) location.replace(dest);
    else location.hash = target;
  }

  function go(delta) {
    history.go(delta);
  }

  function scrollKey(path) {
    return '_vanguard_scroll_' + normalizePathname(path);
  }

  function saveScrollKey(path) {
    try {
      sessionStorage.setItem(scrollKey(path), String(window.scrollY));
    } catch (e) {}
  }

  function restoreScroll(path, forceTopForCrossRoute = false) {
    const key = scrollKey(path);
    try {
      const saved = sessionStorage.getItem(key);
      const y = saved !== null ? parseInt(saved, 10) : NaN;
      requestAnimationFrame(() => {
        if (!forceTopForCrossRoute && !Number.isNaN(y)) window.scrollTo(0, y);
        else window.scrollTo(0, 0);
      });
    } catch (e) {
      requestAnimationFrame(() => window.scrollTo(0, 0));
    }
  }

  let lastRouteForScroll = null;
  function onHashChange() {
    const prev = lastRouteForScroll;
    const next = normalizePathname(parseHash().path);
    saveScrollKey(prev || next);
    render();
    restoreScroll(next, prev && prev !== next);
    lastRouteForScroll = next;
  }

  function onPopState() {
    render();
    const next = normalizePathname(parseHash().path);
    restoreScroll(next, lastRouteForScroll && lastRouteForScroll !== next);
    lastRouteForScroll = next;
  }

  function init(initialRender = true) {
    if (isInitialized) return;
    isInitialized = true;
    window.addEventListener('hashchange', onHashChange);
    window.addEventListener('popstate', onPopState);
    window.addEventListener(
      'scroll',
      () => {
        if (currentPath) saveScrollKey(currentPath);
      },
      { passive: true }
    );
    lastRouteForScroll = normalizePathname(parseHash().path);
    if (initialRender) render();
  }

  router.init = init;
  router.navigate = navigate;
  router.go = go;
  router.current = () => currentPath;
  router.parse = parseHash;

  window.VanguardRouter = router;
})();
```

---

### 2) App wiring (`/opt/axentx/vanguard/frontend/app.js`)
```js
// App: dataset list/detail + URL-synchronized filters/pagination/selection
(function () {
  const R = window.VanguardRouter;
  if (!R) return;

  // Lightweight client-side cache to preserve UI state across route changes
  const cache = {
    datasets: [],
    filters: { name: '' },
    pagination: { page: 1, perPage: 20 },
  };

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function qs(params) {
    const p = new URLSearchParams();
    for (const k in params) if (params[k] != null) p.set(k, params[k]);
    return p.toString() ? '?' + p.toString() : '';
  }

  function parseQueryToState(params) {
    const out = {};
    for (const k of ['name', 'page', 'perPage', 'id']) {
      const v = params.get(k);
      if (v != null) out[k] = isNaN(Number(v)) ? v : Number(v);
    }
    return out;
  }

  function applyQueryToCache(params) {
    const parsed = parseQueryToState(params);
    if (parsed.name != null) cache.filters.name = parsed.name;
    if (parsed.page != null && parsed.page >= 1) cache.pagination.page = parsed.page;
    if (parsed.perPage != null && parsed.perPage >= 1) cache.pagination.perPage = parsed.perPage;
  }

  function renderList() {
    const container = document.getElementById('app');
    if (!container) return;
    const { filters, pagination, datasets } = cache;
    const filtered = datasets.filter((d) =>
      filters.name ? d.name.toLowerCase().includes(filters.name.toLowerCase()) : true
    );
    const start = (pagination.page - 1) * pagination.perPage;
    const pageItems = filtered.slice(start, start + pagination.perPage);

    container.innerHTML = `
      <div class
