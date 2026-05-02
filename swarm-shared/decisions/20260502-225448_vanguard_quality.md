# vanguard / quality

## Final synthesized implementation

**Diagnosis (resolved)**  
- No router → full reloads, broken deep-links, no shareable/bookmarkable URLs.  
- UI state not in URL → reloads lose filters/pagination/dataset selection.  
- No scroll restoration → disorienting back/forward UX.  
- No route config or 404 handling → brittle, inconsistent future routes.  
- Missing loading/code-splitting cues → blank screens and unnecessary upfront JS.

**Chosen approach**  
- Minimal, dependency-free hash router in `src/router.js` with URL-state synchronization and scroll restoration.  
- Framework-agnostic; uses `CustomEvent` for route changes and a tiny scroll-restore contract.  
- Centralized route registration with specificity ordering, param parsing, query-state helpers, and 404 fallback.  
- Integrate into `src/main.js` with route handlers and two-way binding for filter controls.  
- Add small CSS for scroll-smooth restoration UX in `src/index.css`.

---

### `/opt/axentx/vanguard/frontend/src/router.js`
```js
// Minimal hash router with URL-synchronized state and scroll restoration.
// Framework-agnostic; dispatches 'axentx:routechange' with { route, handler, isPop, lastScrollY }.

(function () {
  const ROUTE_CHANGE = 'axentx:routechange';

  function parse() {
    const hash = location.hash || '#/';
    const [path, rawSearch] = hash.slice(1).split('?');
    const searchParams = new URLSearchParams(rawSearch || '');
    return {
      path,
      params: {},
      query: Object.fromEntries(searchParams.entries()),
      rawSearch: rawSearch || '',
      hash,
    };
  }

  function normalizePath(path) {
    return path.replace(/\/+$/, '') || '/';
  }

  function compile(pattern) {
    const keys = [];
    const safe = pattern
      .replace(/[.+?^${}()|[\]\\]/g, '\\$&')
      .replace(/\\\*\\\*/g, '(.*)')
      .replace(/\\\*/g, '(.*)')
      .replace(/\\:([^\/]+)/g, (_, key) => {
        keys.push(key);
        return '([^/]+)';
      });
    const re = new RegExp('^' + safe + '(?:\\?.*)?$');
    return { re, keys };
  }

  const routes = [];

  function register(path, handler) {
    routes.push({ path, handler, ...compile(path) });
    // specificity: static (0) > param (1) > wildcard (2)
    routes.sort((a, b) => {
      const rank = (p) => (p.path.includes('/:') ? 1 : p.path.includes('*') ? 2 : 0);
      return rank(a) - rank(b);
    });
  }

  function match(pathname) {
    const normalized = normalizePath(pathname);
    for (const r of routes) {
      const m = r.re.exec(normalized);
      if (m) {
        const params = {};
        r.keys.forEach((k, i) => {
          params[k] = decodeURIComponent(m[i + 1] || '');
        });
        return { handler: r.handler, params };
      }
    }
    return null;
  }

  function buildHash(path, query) {
    const q = new URLSearchParams(query || {}).toString();
    const normalized = path.startsWith('#') ? path : '#' + path.replace(/^\//, '');
    return q ? normalized + '?' + q : normalized;
  }

  function push(path, query, replace = false) {
    const next = buildHash(path, query);
    if (replace) {
      location.replace(location.pathname + location.search + next);
    } else {
      location.hash = next;
    }
  }

  function replace(path, query) {
    push(path, query, true);
  }

  function go(queryUpdates, replace = false) {
    const current = parse();
    const nextQuery = { ...current.query, ...queryUpdates };
    Object.keys(nextQuery).forEach((k) => nextQuery[k] == null && delete nextQuery[k]);
    if (replace) {
      replace(current.path, nextQuery);
    } else {
      push(current.path, nextQuery);
    }
  }

  let lastScrollY = 0;
  let isPop = false;

  function emit() {
    const parsed = parse();
    const matched = match(parsed.path);
    parsed.params = matched ? matched.params : {};
    // capture scroll before potential DOM update
    if (!isPop) lastScrollY = window.scrollY || window.pageYOffset;

    document.dispatchEvent(
      new CustomEvent(ROUTE_CHANGE, {
        detail: { route: parsed, handler: matched ? matched.handler : null, isPop, lastScrollY },
      }),
    );

    if (matched) {
      matched.handler(parsed);
    } else {
      // 404/fallback: allow app to handle
      document.dispatchEvent(
        new CustomEvent(ROUTE_CHANGE, {
          detail: { route: parsed, handler: null, isPop, lastScrollY },
        }),
      );
    }
    isPop = false;
  }

  window.addEventListener('hashchange', () => {
    isPop = false;
    emit();
  });

  window.addEventListener('popstate', () => {
    isPop = true;
    emit();
  });

  // Restore scroll after handlers render (microtask + rAF)
  document.addEventListener(ROUTE_CHANGE, (e) => {
    const { isPop: pop, lastScrollY: ly } = e.detail;
    requestAnimationFrame(() => {
      if (pop) {
        window.scrollTo(0, ly || 0);
      } else {
        // new navigation: scroll to top unless native anchor
        const id = location.hash.slice(1);
        if (id && !id.startsWith('/') && document.getElementById(id)) return;
        window.scrollTo(0, 0);
      }
    });
  });

  // init
  if (!location.hash) {
    location.replace(location.pathname + location.search + '#/');
  } else {
    emit();
  }

  // Public API
  window.AxentxRouter = {
    ROUTE_CHANGE,
    parse,
    register,
    push,
    replace,
    go,
    on: (fn) => document.addEventListener(ROUTE_CHANGE, (e) => fn(e.detail)),
  };
})();
```

---

### `/opt/axentx/vanguard/frontend/src/main.js`
```js
import './router.js';

// Example views (replace with your real rendering)
function renderDatasetList(query) {
  // Use query for filters/pagination; update DOM here.
  console.log('Rendering dataset list with', query);
}

function renderDatasetDetail(route) {
  console.log('Rendering dataset detail', route.params.id, route.query);
}

function renderNotFound(route) {
  console.warn('No route matched', route.path);
  // optional: render 404 UI
}

// Register routes
window.AxentxRouter.register('/', () => renderDatasetList(window.AxentxRouter.parse().query));
window.AxentxRouter.register('/datasets', () => renderDatasetList(window.AxentxRouter.parse().query));
window.AxentxRouter.register('/datasets/:id', (route) => renderDatasetDetail(route));

// Global handler for unmatched routes / 404
window.AxentxRouter.on((detail) => {
  if (!detail.handler) {
    renderNotFound(detail.route);
  }
});

// Keep filter controls in sync with URL query params
function bindFilterControls() {
  const searchInput = document.getElementById('dataset-search');
  const pageInput = document.getElementById('dataset-page');
  if (!searchInput && !pageInput) return;

  function applyFromURL() {
    const q = window.AxentxRouter.parse().query;
    if (searchInput && q.q !== undefined) searchInput.value = q.q;
    if (pageInput && q.page !== undefined) pageInput.value = q.page;
  }

  function updateURL() {
    const q = {};
    if (searchInput && searchInput.value.trim()) q.q = searchInput.value.trim();
    if (pageInput && page
