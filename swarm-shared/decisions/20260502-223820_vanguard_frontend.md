# vanguard / frontend

## Final synthesized implementation (best parts, contradictions resolved)

**Core decisions**
- Use a single-page app shell at `/opt/axentx/vanguard/frontend/` (both candidates agree).  
- Prefer Vite + native ES modules (Candidate 1) for fast dev/build and clear project structure.  
- Keep runtime tiny: HTMX for progressive enhancement (Candidate 1) + minimal JS router/views (Candidate 2).  
- Make HF CDN-bypass safe: require pre-listed manifests served from `/public/file-list/` to avoid 429s during surrogate-1 ingestion.  
- Surface Lightning Studio reuse/idle-stop status in the header (both candidates) to reduce quota waste and silent failures.  
- Add a clear Top Hub (MOC) landing view (both candidates) to orient new devs and surface the most-connected knowledge node.

---

### File tree to create

```
/opt/axentx/vanguard/frontend/
├── index.html
├── package.json
├── vite.config.js
├── public/
│   └── file-list/            # orchestration places {date}.json here
├── src/
│   ├── main.js
│   ├── router.js
│   ├── api.js
│   └── views/
│       ├── HubView.js
│       ├── TrainingView.js
│       └── IngestView.js
└── README.md
```

---

### index.html

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Knowledge & Training</title>
  <script type="module" src="/src/main.js" defer></script>
</head>
<body>
  <header class="app-header">
    <nav>
      <a href="/" data-link>Top Hub</a>
      <a href="/training" data-link>Training</a>
      <a href="/ingest" data-link>Ingest</a>
    </nav>
    <div id="status-bar" aria-live="polite"></div>
  </header>

  <main id="app" role="main"></main>

  <footer class="app-footer">
    <small>Vanguard — HF CDN-bypass + Lightning Studio reuse</small>
  </footer>
</body>
</html>
```

---

### package.json

```json
{
  "name": "vanguard-frontend",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "htmx.org": "^1.9.0"
  },
  "devDependencies": {
    "vite": "^5.0.0"
  }
}
```

---

### vite.config.js

```js
import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  build: {
    outDir: 'dist',
    emptyOutDir: true
  }
});
```

---

### src/main.js

```js
import { router } from './router.js';
import { initStatusBar } from './api.js';

function navigateTo(url) {
  history.pushState(null, null, url);
  router();
}

document.addEventListener('click', (e) => {
  if (e.target.matches('[data-link]')) {
    e.preventDefault();
    navigateTo(e.target.href);
  }
});

window.addEventListener('popstate', router);

// initialize
initStatusBar();
router();
```

---

### src/router.js

```js
import { HubView } from './views/HubView.js';
import { TrainingView } from './views/TrainingView.js';
import { IngestView } from './views/IngestView.js';

export function router() {
  const app = document.getElementById('app');
  if (!app) return;

  const path = window.location.pathname;

  if (path === '/' || path === '/hub') {
    app.innerHTML = HubView();
    // attach HubView behaviors
    attachHubBehaviors();
  } else if (path === '/training') {
    app.innerHTML = TrainingView();
    attachTrainingBehaviors();
  } else if (path === '/ingest') {
    app.innerHTML = IngestView();
    attachIngestBehaviors();
  } else {
    app.innerHTML = `<h2>Not Found</h2><p>No route for ${path}</p>`;
  }
}

// lazy-attach per-view behaviors to avoid double-binding
function attachHubBehaviors() {
  const btn = document.getElementById('load-files');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const listEl = document.getElementById('file-list');
    if (listEl) listEl.innerHTML = '<li>Loading...</li>';
    const today = new Date().toISOString().slice(0, 10);
    const files = await loadFileList(today);
    if (listEl) {
      listEl.innerHTML = files.length
        ? files.map((f) => `<li>${f.path} <small>(${f.size || 0} bytes)</small></li>`).join('')
        : '<li>No files available</li>';
    }
  });
}

function attachTrainingBehaviors() {
  const btn = document.getElementById('refresh-studio');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const out = document.getElementById('studio-output');
    if (out) out.textContent = 'Refreshing...';
    const studio = await pollStudioStatus('vanguard-training');
    if (out) out.textContent = JSON.stringify(studio, null, 2);
  });
}

function attachIngestBehaviors() {
  // IngestView uses htmx; lightweight JS helpers can be added here if needed
}

// api exports used by views
import { loadFileList, pollStudioStatus } from './api.js';
```

---

### src/api.js

```js
// HF CDN-bypass file list (orchestration writes JSON to /public/file-list/{date}.json)
let FILE_LIST_CACHE = null;

export async function loadFileList(dateFolder) {
  if (FILE_LIST_CACHE) return FILE_LIST_CACHE;
  try {
    const res = await fetch(`/file-list/${dateFolder}.json`, { cache: 'no-store' });
    if (!res.ok) throw new Error('File list unavailable');
    FILE_LIST_CACHE = await res.json(); // [ { path: "...", size: 123 } ]
    return FILE_LIST_CACHE;
  } catch (err) {
    console.warn('Could not load file list:', err);
    return [];
  }
}

// Lightweight proxy to Lightning Studio status (orchestration should expose /api/studio/:name)
export async function pollStudioStatus(studioName) {
  try {
    const res = await fetch(`/api/studio/${encodeURIComponent(studioName)}`, { cache: 'no-store' });
    if (!res.ok) return { status: 'unknown' };
    return res.json(); // { name, status, machine, uptime }
  } catch {
    return { status: 'unreachable' };
  }
}

export function initStatusBar() {
  const bar = document.getElementById('status-bar');
  if (!bar) return;
  setInterval(async () => {
    const studio = await pollStudioStatus('vanguard-training');
    bar.textContent = `Studio: ${studio.status} ${studio.machine ? `(${studio.machine})` : ''}`;
  }, 15000);
}
```

---

### src/views/HubView.js

```js
export function HubView() {
  return `
    <section>
      <h1>Top Hub — MOC</h1>
      <p>Most-connected knowledge node. Review this hub before planning ingestion or training.</p>

      <div id="hub-files">
        <button id="load-files">Load latest file list (HF CDN-bypass)</button>
        <ul id="file-list"></ul>
      </div>
    </section>
  `;
}
```

---

### src/views/TrainingView.js

```js
export function
