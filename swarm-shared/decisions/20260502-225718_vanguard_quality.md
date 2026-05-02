# vanguard / quality

## Final synthesized solution

**Core principle**: Use a minimal, deterministic scroll cache keyed by route identity (`location.key` when available, else `pathname+search+hash`), integrate tightly with router lifecycle, avoid browser default scroll reset, and restore after render+layout settle.

---

### 1. Diagnosis (merged + resolved)

- **Scroll reset on navigation** (list→detail→back) loses user context.
- **No per-route scroll persistence** across SPA navigation and browser back/forward.
- **Scroll jumps on slow render**: content loads after route change, causing wrong scroll targets.
- **Hash router lacks scroll management**, breaking expected back/forward behavior.
- **Missing scroll anchoring** for fixed headers can hide content on anchor/focus scroll.
- **No differentiation** between list (restore position) and detail (scroll to top) behavior.
- **Immediate scroll on route change feels abrupt**; prefer auto for cached restores, avoid forced smoothness that fights user intent.

---

### 2. Proposed change (merged + prioritized)

Add a lightweight scroll manager:

- Save/restore scroll positions keyed by route identity.
- Integrate into router `beforeEach`/`afterEach` (or equivalent) lifecycle.
- Set `history.scrollRestoration = 'manual'` once at startup.
- Restore after render + small debounce for async/lazy content.
- Add CSS `scroll-padding-top` (or `scroll-margin-top`) for fixed header.
- Default behavior:
  - List routes: restore saved position when available; otherwise scroll to top.
  - Detail routes: scroll to top on first visit; do not restore to middle of another item’s detail.
- Keep implementation framework-agnostic and minimal (no new deps).

---

### 3. Implementation (single, concrete plan)

#### File: `src/router/scroll.ts`

```ts
// src/router/scroll.ts
type ScrollKey = string;

const scrollPositions = new Map<ScrollKey, { x: number; y: number }>();

// Deterministic key for scroll cache
export function getScrollKey(
  pathname: string,
  search: string,
  hash: string,
  key?: string
): ScrollKey {
  // Prefer router's unique key for session navigation; fallback to URL-derived key
  return key || `${pathname}${search}${hash}`;
}

// Save current scroll for key
export function saveScrollPosition(key: ScrollKey) {
  scrollPositions.set(key, { x: window.scrollX, y: window.scrollY });
}

// Restore cached position; returns true if restored
export function restoreScrollPosition(
  key: ScrollKey,
  behavior: ScrollBehavior = 'auto'
): boolean {
  const pos = scrollPositions.get(key);
  if (pos) {
    window.scrollTo({ ...pos, behavior });
    return true;
  }
  return false;
}

// Restore or scroll to default (top)
export function restoreOrScrollToDefault(
  key: ScrollKey,
  defaultY: number = 0,
  behavior: ScrollBehavior = 'auto'
) {
  if (!restoreScrollPosition(key, behavior)) {
    window.scrollTo({ top: defaultY, behavior });
  }
}

// Determine if route is a "detail" view (adjust pattern to your routes)
export function isDetailRoute(pathname: string, hash: string): boolean {
  // Examples: /datasets/:id or hash equivalent
  return /\/datasets\/[^/]+$/.test(pathname) || /#\/datasets\/[^/]+/.test(hash);
}

// Initialize once at app start
export function initScrollManager() {
  if (typeof window === 'undefined') return;
  try {
    window.history.scrollRestoration = 'manual';
  } catch {
    // ignore
  }
}

// Call on leaving a route (before navigation)
export function onBeforeRouteLeave(
  pathname: string,
  search: string,
  hash: string,
  key?: string
) {
  const scrollKey = getScrollKey(pathname, search, hash, key);
  saveScrollPosition(scrollKey);
}

// Call after new route rendered (and microtasks/layout settled)
export function onRouteReady(
  pathname: string,
  search: string,
  hash: string,
  key?: string,
  waitMs: number = 60
) {
  const scrollKey = getScrollKey(pathname, search, hash, key);

  // Wait for DOM and async content to settle
  setTimeout(() => {
    if (isDetailRoute(pathname, hash)) {
      // Detail pages: scroll to top on visit (do not restore previous detail scroll)
      window.scrollTo({ top: 0, behavior: 'auto' });
    } else {
      // List routes: restore saved position or top
      restoreOrScrollToDefault(scrollKey, 0, 'auto');
    }
  }, waitMs);
}
```

#### CSS (add to global styles)

```css
/* Prevent fixed header from obscuring anchor/focus targets */
:root {
  --header-height: 64px; /* adjust to your header height */
}

html {
  scroll-padding-top: var(--header-height);
}

/* For broader compatibility */
* {
  scroll-margin-top: var(--header-height);
}
```

#### Integration: `src/router/index.ts` (example)

```ts
// src/router/index.ts
import {
  initScrollManager,
  onBeforeRouteLeave,
  onRouteReady,
  getScrollKey
} from './scroll';

// Call once at app bootstrap
initScrollManager();

let currentKey: string | undefined;

// Adapt to your router's API (hash or history)
router.beforeEach((to, from) => {
  if (from.path) {
    onBeforeRouteLeave(from.path, from.query || '', from.hash || '', from.key);
  }
});

router.afterEach((to) => {
  currentKey = getScrollKey(to.path, to.query || '', to.hash || '', to.key);
  // Increase waitMs if lists/images load async (e.g., 120-200ms)
  onRouteReady(to.path, to.query || '', to.hash || '', to.key, 80);
});
```

---

### 4. Behavior rules (final, actionable)

- **List routes**: Restore saved scroll when navigating back/forward. If no saved position, scroll to top.
- **Detail routes**: Always scroll to top on visit. Do not restore scroll into another item’s detail.
- **Filter/pagination changes**: Because key includes `search` (and optionally a short hash of filter state), different filter/page combinations have different scroll keys, preventing disorienting jumps.
- **Browser back/forward**: Works because positions are saved on leave and restored on arrival (with `auto` behavior for cached restores).
- **Hard reload**: No saved position for that key → scroll to top (expected and non-jarring).
- **Scroll anchoring**: CSS `scroll-padding-top` prevents fixed header overlap.

---

### 5. Verification checklist

- [ ] `history.scrollRestoration === 'manual'` after init.
- [ ] List → detail → back restores list scroll near previous items.
- [ ] List → filter/page change → back restores previous list scroll.
- [ ] Detail pages scroll to top on visit.
- [ ] No visible scroll jank; `auto` behavior used for cached restores.
- [ ] Anchor links and focus targets are not hidden by fixed header.
- [ ] Rapid list→detail→back→detail→back remains consistent.
- [ ] Works with hash router and history router (keying strategy adapts).

---

**Result**: A minimal, deterministic scroll manager that fixes disorienting scroll resets, preserves list context across navigation, respects detail-page expectations, and integrates cleanly into existing router lifecycle.
