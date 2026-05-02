# vanguard / quality

## 1. Diagnosis

- Dataset detail view (`#/datasets/:id`) does not persist filter/pagination/sort in URL → reloads or shared links lose context and default to page 1 / unfiltered state.
- No loading or error UI during dataset fetch → blank screen or frozen UI on slow networks or 404/500 responses.
- Hash router lacks scroll restoration on back/forward → disorienting jumps to top instead of returning to previous scroll position.
- Ephemeral selection state (row/record selection) is not reflected in URL → can’t link to a specific record or preserve selection across reloads.
- Missing canonical routes for dataset detail and list views → no clean deep-linking or bookmarkable URLs (still using hash-only without query params).

## 2. Proposed change

File scope: `/opt/axentx/vanguard/src/routes/DatasetDetail.jsx` (or `.js`) and `/opt/axentx/vanguard/src/lib/router.js` (or equivalent).  
Goal: persist dataset list filters/pagination/sort and detail selection in URL query params, add loading/error UI, and enable scroll restoration for hash navigation.

## 3. Implementation

```bash
# Ensure we're in the project
cd /opt/axentx/vanguard
```

### 3.1 Router: sync query params and restore scroll

`src/lib/router.js` (or create if missing):

```js
// src/lib/router.js
import { createHashHistory } from './history.js';

const history = createHashHistory();

// Restore scroll position for back/forward
const scrollPositions = new Map();

function saveScroll(key) {
  scrollPositions.set(key, { x: window.scrollX, y: window.scrollY });
}

function restoreScroll(key) {
  const pos = scrollPositions.get(key);
  if (pos) {
    window.scrollTo(pos.x, pos.y);
  } else {
    // default: scroll to top for new routes, preserve for same-route query changes
    const sameRoute = key.split('?')[0] === location.hash.split('?')[0];
    if (!sameRoute) window.scrollTo(0, 0);
  }
}

// Parse hash into { path, search, hash }
function parseHash(hash = location.hash) {
  const cleaned = hash.replace(/^#/, '');
  const [path = '', search = ''] = cleaned.split('?');
  return { path, search: search ? `?${search}` : '' };
}

function stringifyQuery(params) {
  const sp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v != null && v !== '') sp.set(k, String(v));
  });
  const qs = sp.toString();
  return qs ? `?${qs}` : '';
}

function parseQuery(search = '') {
  const sp = new URLSearchParams(search.replace(/^\?/, ''));
  const out = {};
  for (const [k, v] of sp.entries()) out[k] = v;
  return out;
}

// Navigate and optionally merge query params
function navigate(to, { replace = false, mergeQuery = true } = {}) {
  const current = parseHash();
  const next = parseHash(to);

  let nextSearch = next.search;
  if (mergeQuery && current.search && !next.search) {
    // preserve current query when navigating to same path without query
    const currPath = current.path;
    const nextPath = next.path;
    if (currPath === nextPath) nextSearch = current.search;
  }

  const newHash = `#${next.path}${nextSearch}`;
  const key = newHash;
  saveScroll(location.hash || key);
  if (replace) {
    location.replace(newHash);
  } else {
    location.hash = newHash;
  }
  // small delay to allow hash to settle before restoring scroll for same-hash updates
  requestAnimationFrame(() => restoreScroll(key));
}

// Subscribe to hash changes
function onHashChange(callback) {
  window.addEventListener('hashchange', () => {
    const key = location.hash || '#/';
    restoreScroll(key);
    callback(parseHash());
  });
}

export const router = {
  navigate,
  parseHash,
  parseQuery,
  stringifyQuery,
  onHashChange,
  get current() { return parseHash(); },
};
```

`src/lib/history.js` (lightweight shim):

```js
// src/lib/history.js
export function createHashHistory() {
  return {
    get location() {
      const { path, search } = parseHash();
      return {
        pathname: path,
        search,
        hash: location.hash,
      };
    },
  };
}

function parseHash(hash = location.hash) {
  const cleaned = hash.replace(/^#/, '');
  const [path = '', search = ''] = cleaned.split('?');
  return { path, search: search ? `?${search}` : '' };
}
```

### 3.2 Dataset list: persist filters/pagination/sort in URL

`src/routes/DatasetList.jsx` (example):

```jsx
// src/routes/DatasetList.jsx
import { useEffect, useState } from 'react';
import { router } from '../lib/router.js';
import { fetchDatasets } from '../api/datasets.js';

export default function DatasetList() {
  const [state, setState] = useState({ items: [], loading: false, error: null });
  const q = router.parseQuery(router.current.search);

  const page = Number(q.page) || 1;
  const pageSize = Number(q.pageSize) || 20;
  const sort = q.sort || 'createdAt';
  const order = q.order || 'desc';
  const filter = q.filter || '';

  useEffect(() => {
    setState((s) => ({ ...s, loading: true, error: null }));
    fetchDatasets({ page, pageSize, sort, order, filter })
      .then((items) => setState({ items, loading: false, error: null }))
      .catch((err) => setState({ items: [], loading: false, error: err.message }));
  }, [page, pageSize, sort, order, filter]);

  function setQuery(params) {
    const next = { page, pageSize, sort, order, filter, ...params };
    router.navigate(`#/datasets?${router.stringifyQuery(next)}`, { mergeQuery: false });
  }

  if (state.error) {
    return (
      <div className="p-4 text-red-600">
        <strong>Error loading datasets:</strong> {state.error}
        <button onClick={() => setQuery({})} className="ml-2 underline">Retry</button>
      </div>
    );
  }

  return (
    <div>
      <div className="mb-4 flex items-center gap-2">
        <input
          value={filter}
          onChange={(e) => setQuery({ filter: e.target.value, page: 1 })}
          placeholder="Filter..."
          className="border px-2 py-1 rounded"
        />
        <select
          value={sort}
          onChange={(e) => setQuery({ sort: e.target.value })}
          className="border px-2 py-1 rounded"
        >
          <option value="createdAt">Created</option>
          <option value="name">Name</option>
        </select>
        <select
          value={order}
          onChange={(e) => setQuery({ order: e.target.value })}
          className="border px-2 py-1 rounded"
        >
          <option value="desc">Desc</option>
          <option value="asc">Asc</option>
        </select>
      </div>

      {state.loading && <div className="p-4 text-gray-600">Loading datasets...</div>}

      {!state.loading && (
        <>
          <ul className="space-y-2">
            {state.items.map((ds) => (
              <li key={ds.id}>
                <a href={`#/datasets/${ds.id}`} className="underline hover:text-blue-700">
                  {ds.name}
                </a>
              </li>
            ))}
          </ul>

          <div className="mt-4 flex items-center gap-2">
            <button
              disabled={page <= 1}
              onClick={() => setQuery({ page: page - 
