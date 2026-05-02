# vanguard / quality

## 1. Diagnosis
- Missing client-side router: hash-based navigation (`#/`, `#/datasets`, `#/datasets/:id`) is absent, causing full-page reloads or 404s on direct access and breaking back/forward and iframe embeds.
- No URL-driven state: selected dataset, filters, and pagination are not reflected in the URL, so links cannot be bookmarked or shared.
- No route-level error boundaries: navigation to invalid routes shows blank or console errors instead of a graceful 404.
- Hard-coded navigation links: dataset links use plain `<a href="...">` that trigger full reloads instead of client-side transitions.
- No scroll restoration or focus management: returning to previous routes loses scroll position and keyboard focus.

## 2. Proposed change
Create `/opt/axentx/vanguard/frontend/router.js` and integrate it into `/opt/axentx/vanguard/frontend/index.html` (or main entry). Scope:
- Add `router.js` with hash-based route definitions and a tiny dispatcher.
- Replace hard `<a>` navigation with `router.navigate()` where applicable.
- Add a minimal layout area (`<main id="app-root">`) for route rendering and a 404 fallback.

## 3. Implementation

### `/opt/axentx/vanguard/frontend/router.js`
```js
// Lightweight hash router for vanguard frontend
// Supports: #/ , #/datasets , #/datasets/:id
(function (global) {
  const routes = [
    { path: '/', handler: renderHome },
    { path: '/datasets', handler: renderDatasets },
    { path: '/datasets/:id', handler: renderDatasetDetail },
  ];

  function parseHash() {
    const hash = location.hash || '#/';
    const path = hash.slice(1);
    const segments = path.split('/').filter(Boolean);
    return { raw: hash, path, segments };
  }

  function matchRoute() {
    const { path, segments } = parseHash();
    for (const r of routes) {
      const parts = r.path.split('/').filter(Boolean);
      if (parts.length !== segments.length) continue;
      const params = {};
      const matched = parts.every((p, i) => {
        if (p.startsWith(':')) {
          params[p.slice(1)] = decodeURIComponent(segments[i]);
          return true;
        }
        return p === segments[i];
      });
      if (matched) return { route: r, params };
    }
    return null;
  }

  function renderNotFound() {
    const root = document.getElementById('app-root');
    if (root) {
      root.innerHTML = `
        <section class="p-6">
          <h1 class="text-2xl font-bold text-red-700">404 — Page not found</h1>
          <p class="mt-2">The requested route was not found.</p>
          <a href="#/" class="mt-4 inline-block text-blue-600 underline">Go home</a>
        </section>
      `;
    }
    document.title = '404 — Vanguard';
  }

  // Stubs — replace with real implementations or wire to existing UI modules
  function renderHome() {
    const root = document.getElementById('app-root');
    if (root) root.innerHTML = '<section class="p-6"><h1 class="text-xl font-semibold">Vanguard Home</h1></section>';
    document.title = 'Vanguard — Home';
  }

  function renderDatasets() {
    const root = document.getElementById('app-root');
    if (root) root.innerHTML = '<section class="p-6"><h1 class="text-xl font-semibold">Datasets</h1></section>';
    document.title = 'Vanguard — Datasets';
  }

  function renderDatasetDetail(params) {
    const root = document.getElementById('app-root');
    if (root) {
      root.innerHTML = `
        <section class="p-6">
          <h1 class="text-xl font-semibold">Dataset: ${escapeHtml(params.id)}</h1>
          <p class="mt-2">Details for dataset ${escapeHtml(params.id)}.</p>
        </section>
      `;
    }
    document.title = `Vanguard — Dataset ${params.id}`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (m) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  }

  function navigate(to, replace = false) {
    if (!to.startsWith('#')) to = '#' + to;
    if (replace) {
      history.replaceState(null, '', to);
    } else {
      history.pushState(null, '', to);
    }
    route();
  }

  function route() {
    const m = matchRoute();
    if (m) {
      try {
        m.route.handler(m.params || {});
      } catch (err) {
        console.error('Route handler error:', err);
        renderNotFound();
      }
    } else {
      renderNotFound();
    }
    // Basic focus management: move focus to app root for screen readers
    const root = document.getElementById('app-root');
    if (root) {
      root.setAttribute('tabindex', '-1');
      root.focus({ preventScroll: true });
    }
  }

  // Public API
  global.Router = {
    navigate,
    route,
    start() {
      window.addEventListener('hashchange', route);
      window.addEventListener('load', route);
      // Intercept same-origin anchor clicks for hash links
      document.addEventListener('click', (e) => {
        const a = e.target.closest('a[href^="#"]');
        if (a && a.host === location.host && a.pathname === location.pathname) {
          e.preventDefault();
          navigate(a.hash.slice(1) || '/');
        }
      });
    },
  };
})(window);
```

### Update `/opt/axentx/vanguard/frontend/index.html`
Add the router script and an app root (place before closing `</body>`):
```html
<!-- Existing content above -->
<main id="app-root" tabindex="-1"></main>

<script src="./router.js"></script>
<script>
  // Start router once DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => Router.start());
  } else {
    Router.start();
  }
</script>
</body>
</html>
```

### Optional: convert a sample dataset link to client-side navigation
Replace any hard dataset links like:
```html
<a href="/datasets/abc123">Dataset</a>
```
with:
```html
<a href="#/datasets/abc123">Dataset</a>
```
(router already intercepts same-origin hash links; this keeps behavior consistent.)

## 4. Verification
1. Open `/opt/axentx/vanguard/frontend/index.html` in a browser (or serve via static server).
2. Click a hash link (`#/`, `#/datasets`, `#/datasets/xyz`) and confirm:
   - No full-page reload (check Network tab; only the initial page load).
   - URL updates and browser history works (Back/Forward).
3. Directly visit `index.html#/datasets/xyz` and confirm the detail view renders.
4. Visit an unknown hash (e.g., `#/unknown`) and confirm the 404 message appears.
5. Confirm focus moves to the main content area after navigation (check by tabbing after route change).
