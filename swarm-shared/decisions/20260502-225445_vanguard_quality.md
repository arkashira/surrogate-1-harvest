# vanguard / quality

## Final Synthesized Implementation

**Diagnosis (resolved)**  
- Missing hash router → full reloads, broken deep links (`#/datasets`, `#/datasets/:id`), unbookmarkable detail views.  
- UI state ephemeral → filters/pagination/selection lost on reload/share.  
- No scroll restoration → back/forward jumps to top.  
- No canonical routes → embeds/bookmarks fail.  
- Query params not synchronized with controls.

**Proposed change (merged)**  
Add one minimal, dependency-free router + URL-state synchronizer to `/opt/axentx/vanguard/frontend/`:

- `router.js` — hashchange/popstate router with canonical routes, guards, query sync, and scroll restoration.  
- Integrate into `index.html` and wire to existing dataset list/detail UI so filters/pagination/selection map to URL and restore on load.  
- Expose a tiny public API for UI controls to drive navigation and query sync.

---

### `/opt/axentx/vanguard/frontend/router.js`
```js
(function () {
  'use strict';

  // ---------- Config ----------
  const ROUTES = [
    { pattern: /^#\/$/, handler: showList },
    { pattern: /^#\/datasets\/?$/, handler: showList },
    { pattern: /^#\/datasets\/([^/?]+)\/?$/, handler: showDetail },
  ];
  const DEFAULT_HASH = '#/datasets';
  const SCROLL_DEBOUNCE_MS = 150;
  const SCROLL_CACHE_TTL_MS = 10 * 60 * 1000; // 10m

  // ---------- URL utils ----------
  function parseQuery(search) {
    const q = (search || '').replace(/^\?/, '');
    if (!q) return {};
    const out = {};
    for (const pair of q.split('&')) {
      const [k, v = ''] = pair.split('=').map(decodeURIComponent);
      out[k] = v;
    }
    return out;
  }

  function stringifyQuery(obj) {
    return Object.entries(obj || {})
      .filter(([, v]) => v != null && v !== '')
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
      .join('&');
  }

  function normalizeHash(raw) {
    const h = (raw || location.hash || DEFAULT_HASH).toString();
    return h.startsWith('#') ? h : '#' + h;
  }

  // ---------- Navigation ----------
  function getHash() {
    return normalizeHash(location.hash);
  }

  function buildUrl(to) {
    let hash = normalizeHash(to.hash || to.path || to);
    const query = stringifyQuery(to.query);
    return hash + (query ? '?' + query : '');
  }

  function navigate(to, replace = false) {
    const url = buildUrl(to);
    if (replace) {
      history.replaceState(null, '', url);
    } else {
      history.pushState(null, '', url);
    }
    route();
  }

  // ---------- Routing ----------
  function route() {
    const hash = getHash();
    const [path, search] = hash.split('?');
    const query = parseQuery(search);

    for (const r of ROUTES) {
      const m = path.match(r.pattern);
      if (m) {
        const params = m.slice(1);
        const named = {};
        if (r.pattern.toString().includes('([^/?]+)')) named.id = params[0];
        return r.handler({ params, named, query, path, raw: hash });
      }
    }
    // fallback
    navigate(DEFAULT_HASH, true);
  }

  // ---------- Scroll restoration ----------
  const scrollCache = new Map(); // key -> { y, ts }

  function scrollKey() {
    return location.pathname + location.search + location.hash;
  }

  function saveScroll() {
    const key = scrollKey();
    scrollCache.set(key, { y: window.scrollY, ts: Date.now() });
  }

  function restoreScroll() {
    const key = scrollKey();
    const saved = scrollCache.get(key);
    const now = Date.now();

    if (saved && now - saved.ts < SCROLL_CACHE_TTL_MS) {
      window.scrollTo(0, saved.y);
    } else {
      // allow per-route defaults via query; otherwise top
      const [, search] = location.hash.split('?');
      const query = parseQuery(search);
      window.scrollTo(0, query && query.scroll === 'top' ? 0 : 0);
    }
  }

  // ---------- Route handlers (adapt to your app) ----------
  function showList({ query }) {
    const page = Math.max(1, parseInt(query.page, 10) || 1);
    const q = query.q || '';
    const sort = query.sort || '';

    // Sync URL if UI-driven query differs (idempotent replace)
    const current = parseQuery(location.hash.split('?')[1] || '');
    const want = { page: String(page), q, sort };
    const changed = Object.keys(want).some((k) => String(current[k] || '') !== String(want[k]));
    if (changed) {
      navigate({ path: '#/datasets', query: want }, true);
      return;
    }

    // Render (hook to your existing UI)
    if (window.Vanguard && window.Vanguard.renderDatasetList) {
      window.Vanguard.renderDatasetList({ page, q, sort });
    }

    restoreScroll();
  }

  function showDetail({ named, query }) {
    const id = named.id;
    if (window.Vanguard && window.Vanguard.renderDatasetDetail) {
      window.Vanguard.renderDatasetDetail({ id, query });
    }
    restoreScroll();
  }

  // ---------- Public API ----------
  window.Vanguard = window.Vanguard || {};
  window.Vanguard.Router = {
    navigate,
    replace: (to) => navigate(to, true),
    go: (n) => history.go(n),
    getState() {
      const hash = getHash();
      const [path, search] = hash.split('?');
      return { path, query: parseQuery(search), hash };
    },
    syncQuery(updates) {
      const current = this.getState();
      const q = { ...current.query, ...updates };
      Object.keys(q).forEach((k) => (q[k] == null || q[k] === '') && delete q[k]);
      navigate({ path: current.path, query: q }, true);
    },
  };

  // ---------- Events ----------
  window.addEventListener('hashchange', route);
  window.addEventListener('popstate', route);

  // Save scroll before page unload and on scroll (debounced)
  window.addEventListener('beforeunload', saveScroll);
  let saveTimer = null;
  window.addEventListener(
    'scroll',
    () => {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(saveScroll, SCROLL_DEBOUNCE_MS);
    },
    { passive: true }
  );

  // ---------- Init ----------
  if (!location.hash) {
    navigate(DEFAULT_HASH, true);
  } else {
    route();
  }
})();
```

---

### `/opt/axentx/vanguard/frontend/index.html`
Add before `</body>`:
```html
<script src="./router.js"></script>
```

---

### Wiring UI controls (examples — adapt to your DOM)
```js
// Pagination buttons
document.querySelectorAll('[data-page]').forEach((el) => {
  el.addEventListener('click', (e) => {
    e.preventDefault();
    window.Vanguard.Router.syncQuery({ page: el.dataset.page });
  });
});

// Search input
const searchInput = document.querySelector('#search-input');
if (searchInput) {
  searchInput.addEventListener('change', (e) => {
    window.Vanguard.Router.syncQuery({ q: e.target.value.trim(), page: 1 });
  });
}

// Dataset selection (detail view)
function selectDataset(id) {
  window.Vanguard.Router.navigate(`#/datasets/${encodeURIComponent(id)}`);
}

// If your detail links are <a>, intercept to avoid full reload
document.addEventListener('click', (e) => {
  const a = e.target.closest
