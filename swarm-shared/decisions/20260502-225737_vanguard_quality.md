# vanguard / quality

## Final Synthesis (Corrected + Actionable)

**Core diagnosis (merged, de-duplicated, prioritized)**
- No per-route scroll restoration: navigating list → detail → back resets scroll to top.
- No scroll anchoring: deep links and returning to lists fail to land on the intended row/section.
- No saved scroll state across reloads: session context (list position + filters) is lost on refresh.
- Layout shifts after render cause jank and broken restoration (async content pushes viewport).
- Modals/overlays don’t preserve body scroll, causing jumps on close.

**Chosen approach**
- Use hash router (existing) and sessionStorage (per-session) for scroll state.
- Key scroll state by normalized route path (exclude hash anchor) so detail views and lists have independent positions.
- Anchor only when explicit (deep link or back to list) and only if target exists; otherwise restore saved scroll.
- Defer restoration until after DOM/layout settle using `requestAnimationFrame` + `ResizeObserver` with timeout fallback.
- Preserve body scroll for modals via `overflow: hidden` + `padding-right` lock and restore exact scroll on close.
- Add stable row IDs in list template and explicit detail container IDs.

---

### 1. Scroll manager
Create `/opt/axentx/vanguard/src/scroll-manager.js`:

```js
// src/scroll-manager.js
// Per-route scroll restoration with anchoring and layout-shift tolerance
const STORAGE_KEY = 'vanguard-scroll-state';
const SETTLE_TIMEOUT = 500; // ms fallback if ResizeObserver doesn't fire

function getState() {
  try {
    return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '{}');
  } catch {
    return {};
  }
}

function setState(state) {
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function getRouteKey() {
  // Normalize: strip leading #/, exclude hash anchor after ?, keep query
  const hash = location.hash.replace(/^#\/?/, '');
  const [pathAndQuery] = hash.split('?');
  const query = hash.includes('?') ? '?' + hash.split('?')[1].replace(/#.*/, '') : '';
  return (pathAndQuery || '/') + query;
}

function getAnchorFromHash() {
  const parts = location.hash.split('#');
  return parts.length > 1 ? parts[1] : null;
}

function saveScrollY(key = getRouteKey()) {
  const state = getState();
  state[key] = { y: window.scrollY, ts: Date.now() };
  setState(state);
}

function restoreScrollY(key = getRouteKey()) {
  const state = getState();
  const entry = state[key];
  if (entry && typeof entry.y === 'number') {
    window.scrollTo(0, entry.y);
    return true;
  }
  return false;
}

function anchorTo(id) {
  if (!id) return false;
  const el = document.getElementById(id);
  if (el) {
    el.scrollIntoView({ behavior: 'auto', block: 'start' });
    return true;
  }
  return false;
}

function waitForSettle(callback) {
  let done = false;
  let timer = setTimeout(() => {
    if (!done) {
      done = true;
      callback();
    }
  }, SETTLE_TIMEOUT);

  const ro = new ResizeObserver(() => {
    if (!done) {
      done = true;
      clearTimeout(timer);
      callback();
    }
  });

  // Observe body for layout shifts; disconnect after settle
  ro.observe(document.body);
  requestAnimationFrame(() => {
    if (!done) {
      // Small extra frame for DOM mutations
      requestAnimationFrame(() => {
        if (!done) {
          done = true;
          clearTimeout(timer);
          ro.disconnect();
          callback();
        }
      });
    }
  });
}

// Modal scroll lock
let previousBodyScroll = 0;
function lockBodyScroll() {
  previousBodyScroll = window.scrollY;
  document.body.style.position = 'fixed';
  document.body.style.top = `-${previousBodyScroll}px`;
  document.body.style.width = '100%';
  document.body.style.overflowY = 'scroll';
}

function unlockBodyScroll() {
  const scrollY = previousBodyScroll;
  document.body.style.position = '';
  document.body.style.top = '';
  document.body.style.width = '';
  document.body.style.overflowY = '';
  window.scrollTo(0, scrollY);
}

// Call on route transition start
function onBeforeRouteChange() {
  saveScrollY();
}

// Call after route render (pass explicit anchorId if known)
function onRouteRendered(anchorId) {
  const hashAnchor = getAnchorFromHash();
  const target = anchorId || hashAnchor;

  waitForSettle(() => {
    if (!target || !anchorTo(target)) {
      // No anchor or anchor failed: restore saved route position
      if (!restoreScrollY()) {
        // No saved position: default browser behavior is fine
        window.scrollTo(0, 0);
      }
    }
  });
}

// Attach to popstate/hashchange for back/forward
window.addEventListener('popstate', () => {
  requestAnimationFrame(() => onRouteRendered());
});
window.addEventListener('hashchange', () => {
  requestAnimationFrame(() => onRouteRendered());
});

export default {
  getRouteKey,
  saveScrollY,
  restoreScrollY,
  anchorTo,
  onBeforeRouteChange,
  onRouteRendered,
  lockBodyScroll,
  unlockBodyScroll,
};
```

---

### 2. Router integration
Update `/opt/axentx/vanguard/src/router.js` (create if absent):

```js
// src/router.js
import scrollManager from './scroll-manager.js';

function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [pathPart, ...queryParts] = raw.split('?');
  const query = queryParts.length ? '?' + queryParts.join('?') : '';
  return { path: pathPart, query, hash: location.hash };
}

export function navigate(to) {
  scrollManager.onBeforeRouteChange();
  location.hash = to.replace(/^#/, '');
}

function onHashChange() {
  const { path } = parseHash();

  // Adapt these branches to your real app
  if (path.startsWith('datasets/')) {
    const id = path.split('/')[1];
    renderDetail(id);
    scrollManager.onRouteRendered(id); // anchor to detail container
  } else if (path === 'datasets' || path === '/') {
    renderList();
    scrollManager.onRouteRendered(); // restore list scroll
  } else {
    renderNotFound();
    scrollManager.onRouteRendered();
  }
}

export function initRouter() {
  window.addEventListener('hashchange', onHashChange);
  // Initial render
  onHashChange();
}

// Replace these stubs with your real components
function renderList() {
  // Ensure rows have id="row-{slug}"
}

function renderDetail(id) {
  // Ensure detail container has id={id}
}

function renderNotFound() {
  // 404 UI
}
```

---

### 3. App entry
Update `/opt/axentx/vanguard/src/main.js` (or bootstrap):

```js
// src/main.js
import { initRouter } from './router.js';

document.addEventListener('DOMContentLoaded', () => {
  initRouter();
});
```

---

### 4. Dataset list: stable row IDs
In your list template/component, ensure each row has a stable, predictable ID:

```html
<div id="row-{{slug}}" class="dataset-row">
  <!-- row content -->
  <a href="#/datasets/{{slug}}">View</a>
</div>
```

- Use the same `{{slug}}` (or ID) for the detail container ID in `renderDetail`.
- For deep links with section anchors, use `#/datasets/{{slug}}#section-name` and ensure the section element has `id="section-name"`.

---

### 5. Modal scroll lock (optional but recommended)
When opening a modal:

```js
import scrollManager from './scroll-manager.js
