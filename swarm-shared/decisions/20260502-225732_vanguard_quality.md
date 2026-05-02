# vanguard / quality

## Final consolidated implementation (strongest parts, resolved contradictions)

**Core principle**: combine Candidate 1’s robust keying/storage/deferred-restore with Candidate 2’s anchor/scroll-margin/SSR-safe progressive strategy. Where they conflict, prefer correctness and concrete actionability.

---

### 1. Diagnosis (merged)
- No persistent scroll restoration: list → detail → back resets scroll to top.
- Missing scroll-key management: dynamic heights, filters, pagination lose position.
- No scroll anchoring for returning to the same logical item.
- Scroll state is ephemeral: reloads/shared links lose position.
- No progressive/deferred restoration: immediate hash transitions jump; DOM/lazy content not ready.
- No `scroll-margin`/`scroll-padding` for anchored detail routes (`:id`) → headings hide behind fixed header.
- No SSR-safe initialization (race between render and scroll restore).

---

### 2. Proposed change (merged)
- File: `/opt/axentx/vanguard/src/scroll-manager.js` (new) — framework-agnostic, minimal.
- Integrate with `/opt/axentx/vanguard/src/router.js` (update/create) to drive scroll manager on navigation.
- Scope:
  - Save/restore scroll positions keyed by `pathname + search` (exclude hash fragment for key).
  - Use `history.state` to carry `{ x, y, anchor }` on push/replace.
  - Restore on `popstate` and after route changes with `requestAnimationFrame` + deferred wait for DOM/lazy stability.
  - Anchor to element `id` or `data-anchor` when present (e.g., `#/datasets?anchor=item-123`).
  - Add `scroll-margin-top` via CSS for anchored detail routes to avoid fixed-header occlusion.
  - SSR-safe: do not restore on server; auto-init on `DOMContentLoaded` with progressive enhancement.

---

### 3. Implementation

#### `/opt/axentx/vanguard/src/scroll-manager.js`

```js
// src/scroll-manager.js
// Scroll manager for hash-router apps (vanguard)
// Usage: const mgr = initScrollManager({ onRouteChange }); mgr.notifyRouteChange(opts)

const STORAGE_KEY = 'vanguard-scroll-state';
const SAVE_DEBOUNCE_MS = 100;
const RESTORE_DEFER_MS = 150; // wait for DOM/lazy images to settle

function getKey() {
  // Use path+search only (ignore hash fragment for storage key)
  const hash = location.hash || '#/';
  const pathSearch = hash.slice(1); // remove leading '#'
  return pathSearch;
}

function saveScroll(key, x = window.scrollX, y = window.scrollY) {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const state = raw ? JSON.parse(raw) : {};
    state[key] = { x, y, ts: Date.now() };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (e) {
    // ignore storage errors (private mode, quota)
  }
}

function loadScroll(key) {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const state = JSON.parse(raw);
    return state[key] || null;
  } catch (e) {
    return null;
  }
}

function clearScroll(key) {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const state = JSON.parse(raw);
    delete state[key];
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (e) {}
}

function findAnchor(anchor) {
  if (!anchor) return null;
  // Try id first, then data-anchor
  return document.getElementById(anchor) || document.querySelector(`[data-anchor="${anchor}"]`);
}

function restoreScroll(options = {}) {
  const { anchor, immediate = false } = options;
  const key = getKey();

  // 1) Try explicit anchor (id or data-anchor)
  if (anchor) {
    const el = findAnchor(anchor);
    if (el) {
      immediate ? el.scrollIntoView() : el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return { restored: true, method: 'anchor' };
    }
  }

  // 2) Try history.state (most recent navigation intent)
  if (history.state && typeof history.state.scrollY === 'number') {
    window.scrollTo({ top: history.state.scrollY, left: history.state.scrollX || 0, behavior: immediate ? 'auto' : 'auto' });
    return { restored: true, method: 'history-state' };
  }

  // 3) Try persisted scroll
  const saved = loadScroll(key);
  if (saved) {
    window.scrollTo({ top: saved.y, left: saved.x, behavior: immediate ? 'auto' : 'auto' });
    return { restored: true, method: 'persisted' };
  }

  return { restored: false, method: null };
}

export function initScrollManager({ onRouteChange } = {}) {
  let saveTimeout = null;
  let lastKey = getKey();

  function onScroll() {
    if (saveTimeout) clearTimeout(saveTimeout);
    saveTimeout = setTimeout(() => {
      saveScroll(getKey(), window.scrollX, window.scrollY);
    }, SAVE_DEBOUNCE_MS);
  }

  function handleRouteChange({ anchor, saveCurrent = true } = {}) {
    const currentKey = getKey();

    // Save current position before leaving (best-effort)
    if (saveCurrent && lastKey && lastKey !== currentKey) {
      saveScroll(lastKey, window.scrollX, window.scrollY);
    }
    lastKey = currentKey;

    // For new navigation entries, set scroll=0 in state so back/forward can differentiate
    // We'll rely on history.state set by router for push/replace
    requestAnimationFrame(() => {
      setTimeout(() => {
        restoreScroll({ anchor, immediate: false });
      }, RESTORE_DEFER_MS);
    });
  }

  // Continuous lightweight scroll saving
  window.addEventListener('scroll', onScroll, { passive: true });

  // Popstate: back/forward
  window.addEventListener('popstate', () => {
    handleRouteChange({ anchor: history.state?.anchor, saveCurrent: false });
  });

  // Public API
  const api = {
    notifyRouteChange(opts) {
      // Ensure history.state has fresh scroll=0 for new entries (router should set this)
      handleRouteChange(opts);
      if (onRouteChange) onRouteChange(opts);
    },
    saveScrollNow() {
      saveScroll(getKey(), window.scrollX, window.scrollY);
    },
    restoreScrollNow(opts) {
      return restoreScroll({ ...opts, immediate: true });
    },
    clearKey(key) {
      clearScroll(key);
    },
    destroy() {
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('popstate', () => {});
    }
  };

  // SSR-safe: only restore after DOM ready
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      window.addEventListener('DOMContentLoaded', () => {
        // Small extra delay for initial images/lazy content
        setTimeout(() => restoreScroll(), RESTORE_DEFER_MS + 50);
      });
    } else {
      setTimeout(() => restoreScroll(), RESTORE_DEFER_MS);
    }
  }

  return api;
}
```

#### `/opt/axentx/vanguard/src/router.js` (updated)

```js
// src/router.js
// Minimal hash-router integration with scroll manager
import { initScrollManager } from './scroll-manager.js';

const scrollManager = initScrollManager();

function getPathFromHash() {
  const hash = location.hash || '#/';
  return hash.slice(1); // remove leading '#'
}

function renderRoute(path) {
  // Existing route rendering logic (keep unchanged)
  // Ensure anchorable elements have id or data-anchor attributes
  // Example: <div id="dataset-123" class="dataset-row">...</div>
  // or: <div data-anchor="dataset-123" class="dataset-row">...</div>
}

function navigate(path, { replace = false, anchor } = {}) {
  const url = `#${path}`;
  //
