# vanguard / discovery

**Final consolidated solution**

**Diagnosis (resolved)**
- No canonical `/datasets/:id` route → deep links 404 and break bookmarking/sharing.
- Discovery filters/search/sort/pagination are ephemeral → reloads or shared links lose context.
- Scroll and focus reset on navigation → disorienting UX on long lists.
- Knowledge/RAG outputs exist but are not surfaced as contextual cards during discovery.
- Missing lightweight routing causes full-page reloads and no route-level analytics.

**Proposed change (single scope)**
Add a minimal hash router and a Discovery Catalog that:
- exposes `/` and `/datasets/:id` (with query params) as shareable, persistent URLs;
- restores scroll and focus on navigation;
- syncs UI state ↔ URL bidirectionally;
- surfaces top-hub and recent market insights (and RAG contextual cards) in the catalog and dataset sidebar/detail;
- emits lightweight route analytics;
- requires zero build changes and works with static file serving (~120–160 lines total).

**Implementation**

`src/router.js` (new)
```js
// Minimal hash router + discovery state sync
// Routes: /, /datasets, /datasets/:id
// Query params: q, sort, page, pageSize, filter, hub, insight

export class Router {
  constructor(routes, store) {
    this.routes = routes;
    this.store = store;
    this.notFound = routes['*'] || null;
    this._scrollPositions = {};
  }

  parse() {
    const hash = (location.hash || '#').slice(1) || '/';
    const [path, rawSearch] = hash.split('?');
    return { path, search: this.parseSearch(rawSearch || ''), raw: hash };
  }

  parseSearch(str) {
    return str
      .split('&')
      .filter(Boolean)
      .reduce((acc, pair) => {
        const [k, v = ''] = pair.split('=').map(decodeURIComponent);
        acc[k] = v;
        return acc;
      }, {});
  }

  serializeSearch(obj) {
    return Object.entries(obj)
      .filter(([, v]) => v != null && v !== '')
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
      .join('&');
  }

  navigate(toPath, params = {}, replace = false) {
    const search = this.serializeSearch(params);
    const hash = toPath + (search ? `?${search}` : '');
    this._saveScrollKey();
    if (replace) history.replaceState(null, '', `#${hash}`);
    else location.hash = hash;
  }

  syncStoreFromRoute({ path, search }) {
    if (!this.store) return;
    const state = this.store.getState();
    const next = { ...state };

    // Path
    const idMatch = path.match(/^\/datasets\/([^/?]+)$/);
    next.selectedDatasetId = idMatch ? decodeURIComponent(idMatch[1]) : null;
    next.isCatalog = !next.selectedDatasetId && (path === '/' || path === '/datasets' || path === '');

    // Query -> discovery controls
    if (search.q !== undefined) next.query = search.q;
    if (search.sort !== undefined) next.sort = search.sort;
    if (search.page !== undefined) next.page = Math.max(1, parseInt(search.page, 10) || 1);
    if (search.pageSize !== undefined) next.pageSize = Math.max(1, parseInt(search.pageSize, 10) || 20);
    if (search.filter !== undefined) next.filter = search.filter;

    // Catalog hints
    if (search.hub !== undefined) next.topHub = search.hub;
    if (search.insight !== undefined) next.recentInsight = search.insight;

    this.store.setState(next, { fromRoute: true });
  }

  route() {
    const parsed = this.parse();
    const handler = this.routes[parsed.path] || this.routes[parsed.path.replace(/\/[^/]+$/, '/:id')] || this.notFound;
    this.syncStoreFromRoute(parsed);
    if (handler) handler(parsed);
    this.recordPageview(parsed);
    this.restoreScroll();
    this.restoreFocus();
  }

  recordPageview({ path, search }) {
    try {
      const page = `/#${path}${search ? `?${search}` : ''}`;
      if (window.gtag) window.gtag('config', 'G-XXXXXX', { page_path: page, page_location: location.href });
      window.dispatchEvent(new CustomEvent('routechange', { detail: { path, search, page } }));
    } catch (e) {
      // noop
    }
  }

  _saveScrollKey() {
    try {
      const key = this.parse().path;
      this._scrollPositions[key] = window.scrollY;
    } catch (e) {
      // noop
    }
  }

  saveScrollKey() {
    this._saveScrollKey();
  }

  restoreScroll() {
    try {
      const key = this.parse().path;
      const y = this._scrollPositions[key] || 0;
      requestAnimationFrame(() => window.scrollTo(0, y));
    } catch (e) {
      // noop
    }
  }

  restoreFocus() {
    try {
      const state = this.store && this.store.getState();
      // Focus dataset detail when present, otherwise main catalog region
      const targetId = state && state.selectedDatasetId ? 'dataset-detail' : 'catalog-main';
      const el = document.getElementById(targetId);
      if (el) {
        el.setAttribute('tabindex', '-1');
        el.focus({ preventScroll: true });
      }
    } catch (e) {
      // noop
    }
  }

  start() {
    window.addEventListener('hashchange', () => this.route());
    window.addEventListener('popstate', () => this.route());
    window.addEventListener('beforeunload', () => this.saveScrollKey());
    this.route();
  }
}

export function datasetPath(id, query = {}) {
  const search = Object.entries(query)
    .filter(([, v]) => v != null && v !== '')
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
    .join('&');
  return `#/datasets/${encodeURIComponent(id)}${search ? `?${search}` : ''}`;
}

export function catalogPath(query = {}) {
  const search = Object.entries(query)
    .filter(([, v]) => v != null && v !== '')
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
    .join('&');
  return search ? `#/datasets?${search}` : '#/datasets';
}
```

`src/main.js` (or `src/App.js`) — wire-up
```js
import { Router, datasetPath, catalogPath } from './router.js';

// Lightweight store (replace with your existing state manager)
const store = {
  state: {
    query: '',
    sort: 'relevance',
    page: 1,
    pageSize: 20,
    filter: '',
    selectedDatasetId: null,
    isCatalog: true,
    topHub: '',
    recentInsight: '',
    // RAG/contextual cards
    hubCard: null,
    relatedDocs: []
  },
  listeners: [],
  getState() { return { ...this.state }; },
  setState(patch, meta = {}) {
    Object.assign(this.state, patch);
    if (!meta?.fromRoute) this._pushToRoute();
    this.listeners.forEach((l) => l(this.state));
  },
  subscribe(fn) { this.listeners.push(fn); },
  _pushToRoute() {
    const s = this.state;
    const qs = {
      q: s.query || null,
      sort: s.sort,
      page: s.page > 1 ? s.page : null,
      pageSize: s.pageSize !== 20 ? s.pageSize : null,
      filter: s.filter || null,
      hub: s.topHub || null,
      insight: s.recentInsight || null
    };
    const path = s.selectedDatasetId ? `#/datasets/${encodeURIComponent(s.selectedDatasetId)}` : catalogPath(qs).slice(1);
    const search =
