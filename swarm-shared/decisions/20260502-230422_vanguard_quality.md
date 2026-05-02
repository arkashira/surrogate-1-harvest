# vanguard / quality

## Final synthesized implementation (correctness + actionability)

### 1. Diagnosis (resolved)

- **Scroll restoration broken** on back/forward and hash navigations → disorienting jumps.
- **List state is ephemeral** (page, pageSize, sort, dir, filters) → reloads and shared links lose context.
- **Detail view cannot be deep-linked** and does not preserve originating list context.
- **No loading/error UI** during async transitions → perceived stalls/broken flows.
- **Missing scroll-to-anchor** for hash links and scroll-to-top on new navigations.

### 2. Router: scroll restoration + hash handling (src/lib/router.js)

```js
// src/lib/router.js
// Call once after router/history is initialized

function getAnchorPosition(hash) {
  if (!hash) return null;
  try {
    const el = document.querySelector(hash);
    if (el) return { x: 0, y: Math.round(el.getBoundingClientRect().top + window.scrollY) };
  } catch (e) {
    // ignore invalid selector
  }
  return null;
}

function saveScrollPos(key) {
  try {
    sessionStorage.setItem(
      `scrollpos:${key}`,
      JSON.stringify({ x: window.scrollX, y: window.scrollY, ts: Date.now() })
    );
  } catch (e) {
    // ignore storage errors
  }
}

function restoreScrollPos(key, hash) {
  const hashPos = getAnchorPosition(hash);
  if (hashPos) {
    // Prefer anchor for hash navigations (including back/forward)
    window.scrollTo(hashPos.x, hashPos.y);
    return;
  }

  try {
    const raw = sessionStorage.getItem(`scrollpos:${key}`);
    if (raw) {
      const { x, y } = JSON.parse(raw);
      window.scrollTo(x, y);
      return;
    }
  } catch (e) {
    // ignore
  }

  // Default: new navigation -> top
  window.scrollTo(0, 0);
}

// Handle browser back/forward
window.addEventListener('popstate', () => {
  const key = location.pathname + location.search;
  const hash = location.hash;
  // Allow DOM to settle before restoring position
  requestAnimationFrame(() => restoreScrollPos(key, hash));
});

// Smooth same-page hash clicks (opt-in for anchor links)
document.addEventListener('click', (e) => {
  const anchor = e.target.closest && e.target.closest('a[href^="#"]');
  if (!anchor) return;
  const href = anchor.getAttribute('href');
  if (href === '#') return;
  const target = document.querySelector(href);
  if (target) {
    e.preventDefault();
    const hash = href;
    history.pushState(null, '', hash);
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
});

// Expose helpers for programmatic navigation flows
export function beforeNavigation() {
  const key = location.pathname + location.search;
  saveScrollPos(key);
}

export function afterNavigation(toPath) {
  const key = toPath || location.pathname + location.search;
  const hash = location.hash;
  requestAnimationFrame(() => restoreScrollPos(key, hash));
}
```

### 3. Dataset list: URL-driven state (src/pages/DatasetList.js)

```js
// src/pages/DatasetList.js
// Keep this file framework-agnostic (adapt imports to Svelte/React/Vue as needed).

const DEFAULTS = {
  page: 1,
  pageSize: 20,
  sort: 'createdAt',
  dir: 'desc',
  filters: {}
};

function parseStateFromURL() {
  const p = new URLSearchParams(location.search);
  const page = Math.max(1, parseInt(p.get('page') || '1', 10));
  const pageSize = Math.max(1, parseInt(p.get('pageSize') || '20', 10));
  const sort = p.get('sort') || DEFAULTS.sort;
  const dir = p.get('dir') || DEFAULTS.dir;
  let filters = DEFAULTS.filters;
  try {
    const raw = p.get('filters');
    if (raw) filters = JSON.parse(decodeURIComponent(raw));
  } catch (e) {
    // ignore malformed filters
  }
  return { page, pageSize, sort, dir, filters };
}

function pushStateToURL(state) {
  const p = new URLSearchParams();
  p.set('page', String(state.page));
  p.set('pageSize', String(state.pageSize));
  p.set('sort', state.sort);
  p.set('dir', state.dir);
  if (state.filters && Object.keys(state.filters).length) {
    p.set('filters', encodeURIComponent(JSON.stringify(state.filters)));
  }
  const search = p.toString();
  const newURL = `${location.pathname}?${search}${location.hash || ''}`;
  if (`?${search}` !== location.search) {
    history.replaceState(null, '', newURL);
  }
}

function setStateAndURL(next) {
  const prev = parseStateFromURL();
  const updated = { ...prev, ...next };
  pushStateToURL(updated);
  return updated;
}

// Framework hook: call on mount and after navigations
export function mountDatasetList({ onFetch } = {}) {
  const state = parseStateFromURL();
  if (onFetch) onFetch(state);
  afterNavigation(location.pathname + location.search);
  return state;
}

// UI actions (call these from your component handlers)
export function goToPage(page) {
  const s = setStateAndURL({ page });
  return s;
}

export function setPageSize(pageSize) {
  const s = setStateAndURL({ pageSize, page: 1 });
  return s;
}

export function setSort(sort, dir) {
  const s = setStateAndURL({ sort, dir, page: 1 });
  return s;
}

export function setFilters(filters) {
  const s = setStateAndURL({ filters, page: 1 });
  return s;
}
```

### 4. Dataset detail: preserve list context in links

- When rendering list rows, include minimal list state in detail links so the back stack preserves context:

```html
<!-- Example (adapt to your templating) -->
<a href="/datasets/${row.id}?page=${state.page}&pageSize=${state.pageSize}&sort=${state.sort}&dir=${state.dir}">
  ${row.name}
</a>
```

- On detail mount, you may optionally stash the referring list params (e.g., in sessionStorage) if you need custom “return to list” behavior beyond browser history. The router’s scroll/state restoration above will handle back navigation automatically when list state is in the URL.

### 5. Loading and error UI (minimal, required)

- Add a simple loading indicator that shows while dataset fetches are pending.
- Show an inline error banner on fetch failure with a retry action.
- Tie visibility to the same async flow that calls `onFetch`/`fetchDatasets`.

### 6. Verification checklist

- [ ] Back/forward navigations restore scroll position (no jump-to-top).  
- [ ] Hash links scroll smoothly to the target element; reload with hash scrolls to it.  
- [ ] List controls (page/sort/filters) update the URL immediately.  
- [ ] Copying and opening a list URL in a new tab restores identical state.  
- [ ] Reloading a list page preserves state.  
- [ ] Detail opened from list returns to the same list page/sort/filters on browser back.  
- [ ] Loading indicator appears during fetches; errors show actionable UI.
