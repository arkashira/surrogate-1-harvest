# vanguard / quality

## Final Synthesized Solution

**Core diagnosis (merged, de-duplicated, prioritized by impact)**
- Route transitions reset scroll to top (back/forward, list↔detail), breaking navigation ergonomics.
- No scroll-state persistence: returning to a list loses position and visible-item context.
- Dynamic updates (filters/sort/pagination) cause jarring viewport jumps (no anchoring).
- No handling for explicit “scroll-to-top” intent vs. deep-link/back navigation.
- No progressive reveal; abrupt content pop-in on slower loads.
- Missing scroll-linked URL/state prevents sharable deep positions and robust restoration.

**Guiding principles for correctness + actionability**
- Use `history.scrollRestoration = 'manual'` and manage state explicitly.
- Persist per-route scroll state in `sessionStorage` with TTL and size limit.
- Restore scroll after paint; avoid layout thrashing; throttle saves.
- Distinguish intent:
  - Back/forward/popstate → restore saved position.
  - Deep link/new tab → preserve browser default (do not force top).
  - Explicit same-route or top-level nav click → smooth scroll-to-top.
- Anchor viewport on dynamic list changes (scroll anchoring via stable sentinel).
- Add lightweight progressive reveal (fade/slide) to reduce pop-in.
- Keep router-agnostic; adapt to hash or history-based routing.

---

### Implementation

**1) Create `/opt/axentx/vanguard/src/lib/scroll-manager.js`**

```js
// Lightweight, router-agnostic scroll manager
// Features:
// - Per-route scroll persistence (sessionStorage, TTL, size cap)
// - Back/forward restoration
// - Smooth scroll-to-top on explicit nav clicks
// - Scroll anchoring for dynamic list updates
// - Progressive fade-in for main content

const STORAGE_KEY = 'vanguard:scroll-state';
const MAIN_SELECTOR = 'main';
const NAV_LINK_SELECTOR = 'a[href^="#"], a[data-nav]';
const TTL_MS = 10 * 60 * 1000; // 10m
const MAX_ENTRIES = 64;

function getKey(route) {
  return `${STORAGE_KEY}:${route}`;
}

function getRoute() {
  // Prefer pathname for history API; fallback to hash
  const path = window.location.pathname || '';
  const hash = window.location.hash || '';
  return (path !== '/' && path) || hash || '#/';
}

function saveScroll(route) {
  try {
    const key = getKey(route);
    const state = { x: window.scrollX, y: window.scrollY, ts: Date.now() };
    const raw = sessionStorage.getItem(STORAGE_KEY);
    const index = raw ? JSON.parse(raw) : {};
    index[key] = state;
    // simple size cap
    const keys = Object.keys(index);
    if (keys.length > MAX_ENTRIES) {
      keys.sort((a, b) => (index[a].ts || 0) - (index[b].ts || 0));
      for (let i = 0; i < keys.length - MAX_ENTRIES + 1; i++) delete index[keys[i]];
    }
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(index));
  } catch (e) {
    // ignore storage errors
  }
}

function loadScroll(route) {
  try {
    const key = getKey(route);
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const index = JSON.parse(raw);
    const state = index && index[key];
    if (!state) return null;
    if (Date.now() - (state.ts || 0) > TTL_MS) {
      delete index[key];
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(index));
      return null;
    }
    return { x: state.x || 0, y: state.y || 0 };
  } catch (e) {
    return null;
  }
}

function scrollToTop(behavior = 'smooth') {
  window.scrollTo({ top: 0, left: 0, behavior });
}

function fadeInMain() {
  const main = document.querySelector(MAIN_SELECTOR);
  if (!main) return;
  // Avoid interrupting user reading: only fade on navigation-initiated changes
  const wasVisible = main.style.opacity !== '0';
  if (wasVisible) {
    main.style.opacity = 0;
    main.style.transition = 'opacity 220ms ease';
    requestAnimationFrame(() => {
      main.style.opacity = 1;
    });
  }
}

// Scroll anchoring for dynamic list updates
// Place a sentinel at current scrollTop before update; after update,
// adjust scrollTop by delta of sentinel's new position.
function createScrollAnchor() {
  const id = 'vanguard-scroll-anchor';
  let sentinel = document.getElementById(id);
  if (!sentinel) {
    sentinel = document.createElement('div');
    sentinel.id = id;
    sentinel.style.position = 'absolute';
    sentinel.style.width = '1px';
    sentinel.style.height = '1px';
    sentinel.style.pointerEvents = 'none';
    sentinel.style.top = `${window.scrollY}px`;
    sentinel.style.left = '0';
    document.body.appendChild(sentinel);
  } else {
    sentinel.style.top = `${window.scrollY}px`;
  }
  return sentinel;
}

function applyScrollAnchor(sentinel) {
  if (!sentinel || !sentinel.parentNode) return;
  const delta = sentinel.getBoundingClientRect().top - (parseFloat(sentinel.style.top) || window.scrollY);
  if (Math.abs(delta) > 1) {
    window.scrollBy(0, delta);
  }
  try { sentinel.remove(); } catch (e) {}
}

export function initScrollManager({ anchorOnUpdate = true } = {}) {
  // Disable browser auto-restore
  if ('scrollRestoration' in window.history) {
    window.history.scrollRestoration = 'manual';
  }

  const route = getRoute();
  const saved = loadScroll(route);

  // Initial load handling
  // If deep link with hash fragment, allow browser default.
  // Otherwise, if no saved position, start at top.
  const hasHashTarget = window.location.hash && window.location.hash !== '#' && window.location.hash !== '#/';
  if (!saved && !hasHashTarget) {
    scrollToTop('auto');
  } else if (saved) {
    requestAnimationFrame(() => {
      window.scrollTo(saved.x, saved.y);
    });
  }

  // Save scroll on navigation (hashchange/popstate handled below)
  // and before pagehide
  let scrollSaveRAF = null;
  const scheduleSave = () => {
    if (scrollSaveRAF) cancelAnimationFrame(scrollSaveRAF);
    scrollSaveRAF = requestAnimationFrame(() => {
      saveScroll(getRoute());
    });
  };

  window.addEventListener('scroll', scheduleSave, { passive: true });
  window.addEventListener('pagehide', () => saveScroll(getRoute()));

  // Detect route changes: hashchange for hash routers; for history-based routers,
  // rely on popstate + manual trigger from router (if available).
  window.addEventListener('hashchange', () => {
    saveScroll(route); // previous route (captured closure)
    fadeInMain();
  });

  // Popstate: back/forward
  window.addEventListener('popstate', () => {
    const current = getRoute();
    const savedPop = loadScroll(current);
    if (savedPop) {
      window.scrollTo(savedPop.x, savedPop.y);
    } else {
      // sensible default
      if (current === '/' || current === '#/' || current === '#') {
        scrollToTop('auto');
      }
    }
  });

  // Nav clicks: scroll-to-top for same-route or top-level '#'
  document.addEventListener('click', (e) => {
    const anchor = e.target.closest(NAV_LINK_SELECTOR);
    if (!anchor) return;
    const href = anchor.getAttribute('href') || anchor.getAttribute('data-nav') || '';
    const current = getRoute();

    // Same-route or top '#'
    if (href === '#' || href === current || href === window.location.hash) {
      e.preventDefault();
      if (href === '#') {
        // allow hash update if needed
        if (window.location.hash !== '#') window.location.hash = '
