# vanguard / frontend

## 1. Diagnosis

- Missing client-side router for hash-based navigation (`#/`, `#/datasets`, `#/datasets/:id`) — links reload or 404 on direct access.
- No URL-driven state synchronization — UI state (selected dataset, filters) is not reflected in the URL, breaking share/bookmark and back-button expectations.
- No lightweight data layer for dataset listing/detail — repeated inline fetches and no caching produce duplicated requests and slow navigation.
- Missing progressive enhancement for dataset cards — skeleton placeholders and error boundaries absent; perceived performance and resilience are weak.
- Build/start scripts not optimized for frontend iteration — no dev server with hash-routing fallback or hot-reload for fast frontend-only changes.

## 2. Proposed change

Scope: frontend-only, <2h delivery.

- Add a minimal hash router + URL state manager (`src/router.js`).
- Add a dataset store with CDN-bypass-aware fetcher (`src/store/datasets.js`) that uses `list_repo_tree`-style file-list JSON when available and falls back to per-file CDN fetches.
- Wire router into main app (`src/main.js`) and render dataset list/detail views (`src/views/DatasetList.js`, `src/views/DatasetDetail.js`).
- Add lightweight UI polish: skeleton cards, error boundary, and back-button handling.
- Update `package.json` scripts to include a simple static dev server (or document `npx serve` usage) with hash fallback.

## 3. Implementation

Below are concrete file-level changes. Assume project root is `/opt/axentx/vanguard` and frontend source is in `src/`.

### src/router.js
```js
// Minimal hash router: maps #/path and #/datasets/:id
export function parseHash() {
  const hash = location.hash.slice(1) || '/';
  const [base, id] = hash.split('/').filter(Boolean);
  if (base === 'datasets' && id) return { route: 'dataset', id };
  if (base === 'datasets') return { route: 'datasets' };
  return { route: 'home' };
}

export function navigate(to) {
  // to: '/', '/datasets', '/datasets/abc123'
  location.hash = to.replace(/^\//, '');
}

export function onRouteChange(callback) {
  window.addEventListener('hashchange', () => callback(parseHash()), false);
  // initial
  callback(parseHash());
}
```

### src/store/datasets.js
```js
// CDN-bypass dataset store: uses file-list.json when available, otherwise CDN direct
const CDN_ROOT = 'https://huggingface.co/datasets';
const REPO = 'your-org/your-dataset-repo'; // TODO: parameterize or config

let fileListCache = null;

export async function loadFileList(dateFolder) {
  if (fileListCache) return fileListCache;
  try {
    const res = await fetch(`${CDN_ROOT}/${REPO}/resolve/main/${dateFolder}/file-list.json`);
    if (!res.ok) throw new Error('No file-list');
    fileListCache = await res.json();
  } catch {
    // fallback: minimal tree via CDN (non-recursive) — client can't list tree without API.
    // Caller should provide file-list.json during ingestion (recommended).
    fileListCache = [];
  }
  return fileListCache;
}

export async function fetchDatasetFile(filePath) {
  const url = `${CDN_ROOT}/${REPO}/resolve/main/${filePath}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch ${filePath}: ${res.status}`);
  return res.text();
}

export async function getDatasetMetadata(slug) {
  // lightweight metadata: try meta.json per dataset folder
  try {
    const text = await fetchDatasetFile(`datasets/${slug}/meta.json`);
    return JSON.parse(text);
  } catch {
    return { slug, title: slug, description: '' };
  }
}
```

### src/views/DatasetList.js
```js
import { navigate } from '../router.js';
import { loadFileList } from '../store/datasets.js';

export function renderDatasetList(container, dateFolder = 'batches/mirror-merged') {
  container.innerHTML = '<div class="skeleton-list" id="ds-list"></div>';
  const listEl = document.getElementById('ds-list');

  loadFileList(dateFolder).then((files) => {
    // files expected: array of { path, size } or just paths
    const datasetPaths = (files || [])
      .map((f) => (typeof f === 'string' ? f : f.path))
      .filter((p) => p && p.startsWith('datasets/') && p.endsWith('.parquet'))
      .slice(0, 50);

    if (datasetPaths.length === 0) {
      listEl.innerHTML = '<p class="empty">No datasets found. Ensure file-list.json exists in the date folder.</p>';
      return;
    }

    listEl.innerHTML = datasetPaths
      .map(
        (p) => `
      <div class="card dataset-card" data-path="${p}">
        <div class="skeleton-text title"></div>
        <div class="skeleton-text subtitle"></div>
      </div>
    `
      )
      .join('');

    // progressive populate titles
    datasetPaths.forEach(async (path, i) => {
      const slug = path.split('/').pop().replace('.parquet', '');
      const card = listEl.children[i];
      try {
        // lightweight: use filename as title; optionally fetch meta.json
        card.querySelector('.title').textContent = slug;
        card.querySelector('.subtitle').textContent = path;
      } catch {
        card.querySelector('.title').textContent = slug;
      }
      card.addEventListener('click', () => navigate(`/datasets/${slug}`));
    });
  });
}
```

### src/views/DatasetDetail.js
```js
import { getDatasetMetadata } from '../store/datasets.js';

export async function renderDatasetDetail(container, slug) {
  container.innerHTML = '<div class="skeleton-card"><div class="skeleton-text title"></div><div class="skeleton-text body"></div></div>';
  try {
    const meta = await getDatasetMetadata(slug);
    container.innerHTML = `
      <h1>${escapeHtml(meta.title)}</h1>
      <p>${escapeHtml(meta.description || '')}</p>
      <pre class="meta-json">${escapeHtml(JSON.stringify(meta, null, 2))}</pre>
      <button id="back-btn">← Back</button>
    `;
    document.getElementById('back-btn').addEventListener('click', () => history.back());
  } catch (err) {
    container.innerHTML = `<div class="error">Failed to load dataset: ${escapeHtml(err.message)}</div>`;
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
```

### src/main.js
```js
import { onRouteChange } from './router.js';
import { renderDatasetList } from './views/DatasetList.js';
import { renderDatasetDetail } from './views/DatasetDetail.js';

const app = document.getElementById('app') || document.body;

onRouteChange((route) => {
  app.innerHTML = '';
  if (route.route === 'home') {
    app.innerHTML = '<h1>Vanguard</h1><p><a href="#/datasets">Browse datasets</a></p>';
  } else if (route.route === 'datasets' && !route.id) {
    renderDatasetList(app);
  } else if (route.route === 'dataset' && route.id) {
    renderDatasetDetail(app, route.id);
  } else {
    app.innerHTML = '<h1>Not found</h1>';
  }
});
```

### styles.css (additions)
```css
.skeleton-list, .skeleton-card { background: #f6f7f8; border-radius: 6px; padding: 12px; margin-bottom: 8px; }
.skeleton-text { background: #e8eaed; border-radius: 4px; height: 14px; margin-bottom: 6px; }
.card.dataset-card { cursor: pointer; border: 1px solid #e5e7eb; padding: 12px; border-radius
