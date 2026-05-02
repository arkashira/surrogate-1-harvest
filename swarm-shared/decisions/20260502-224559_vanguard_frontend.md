# vanguard / frontend

## Final Synthesized Implementation

**Chosen scope:** `/opt/axentx/vanguard/frontend/` (create if missing)

This merges the strongest, non-contradictory parts of both proposals and resolves conflicts in favor of correctness, reliability, and concrete actionability:

- **CDN-first, no HF API**: always use `https://huggingface.co/datasets/{owner}/{repo}/resolve/main/...` (never `/api/`).  
- **Persisted `file-list.json`**: required artifact; repo must include it at repo-root. Frontend fetches it once and caches in `localStorage` with 5-minute TTL to avoid repeated network hits and 429s.  
- **Progressive loading**: list → metadata → streamed sample rows (with limit + graceful partial render). Never block the entire view on a large file.  
- **Hash router with validation**: `#/`, `#/datasets`, `#/datasets/:owner/:repo/:path*`. Invalid owner/repo/paths render clear errors instead of blank views.  
- **Schema projection**: normalize rows to `{prompt,response}` on the client for surrogate-1 compatibility.  
- **Local dev**: Vite-based dev/build/preview scripts.  

---

### 1. Project structure

```bash
cd /opt/axentx/vanguard
mkdir -p frontend/public/data frontend/src/views frontend/src/lib
```

---

### 2. package.json

`/opt/axentx/vanguard/frontend/package.json`

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
  "devDependencies": {
    "vite": "^5.0.0"
  }
}
```

---

### 3. Public entry + static assets

`/opt/axentx/vanguard/frontend/public/index.html`

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Dataset Explorer</title>
  <link rel="stylesheet" href="/styles.css" />
</head>
<body>
  <div id="app"></div>
  <script type="module" src="/src/main.js"></script>
</body>
</html>
```

`/opt/axentx/vanguard/frontend/public/styles.css`

```css
:root{--bg:#0f1724;--card:#0b1220;--muted:#6b7280;--accent:#10b981;--border:#1e293b;--text:#e6eef6}
*{box-sizing:border-box}
body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased}
.container{max-width:980px;margin:0 auto;padding:24px}
.card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.card{display:block;background:var(--card);border:1px solid var(--border);padding:16px;border-radius:8px;text-decoration:none;color:var(--text);transition:transform .12s ease,box-shadow .12s ease}
.card:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.35)}
.muted{color:var(--muted)}
.small{font-size:13px}
.breadcrumb a{color:var(--accent);text-decoration:none;font-size:14px}
.breadcrumb span{color:var(--muted);font-size:14px}
.file-row{display:flex;gap:12px;padding:10px;border-bottom:1px solid var(--border);font-size:13px}
.file-row .field{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.error{color:#ef4444;padding:12px;background:rgba(239,68,68,.08);border-radius:6px;border:1px solid rgba(239,68,68,.12)}
.loading{color:var(--muted);padding:18px}
.meta{font-size:13px;color:var(--muted);margin-bottom:8px}
```

---

### 4. Core router (hash-based, validated)

`/opt/axentx/vanguard/frontend/src/router.js`

```js
import { DatasetList } from './views/DatasetList.js';
import { DatasetView } from './views/DatasetView.js';

function parseHash() {
  const raw = (location.hash || '#').replace(/^#/, '') || '/';
  const parts = raw.split('/').filter(Boolean); // ['datasets','owner/repo','path','to','file.parquet']
  return { raw, parts };
}

export const router = {
  resolve() {
    const { parts } = parseHash();
    if (parts[0] === 'datasets') {
      if (!parts[1]) return DatasetList();
      const [owner, repo, ...rest] = parts[1].split('/').concat(parts.slice(2));
      if (!owner || !repo) return renderError('Invalid dataset reference. Expected owner/repo.');
      const path = rest.join('/') || '';
      return DatasetView({ owner, repo, path });
    }
    return DatasetList();
  }
};

function renderError(message) {
  const el = document.createElement('div');
  el.className = 'container';
  el.innerHTML = `<div class="error">${escapeHtml(message)}</div>`;
  return el;
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
```

---

### 5. CDN client with cache and progressive row streaming

`/opt/axentx/vanguard/frontend/src/lib/hfCdn.js`

```js
const CDN = (owner, repo) => `https://huggingface.co/datasets/${owner}/${repo}`;

function cacheKey(kind, owner, repo, extra = '') {
  return `vanguard:${kind}:${owner}/${repo}${extra ? ':' + extra : ''}`;
}

function cachedGet(key, fn, ttlMs = 5 * 60 * 1000) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fn();
    const { t, v } = JSON.parse(raw);
    if (Date.now() - t > ttlMs) return fn();
    return Promise.resolve(v);
  } catch {
    return fn();
  }
}

function setCache(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify({ t: Date.now(), v: value }));
  } catch {
    // ignore storage limits
  }
}

export async function fetchFileList(owner, repo) {
  const key = cacheKey('file-list', owner, repo);
  return cachedGet(key, async () => {
    const url = `${CDN(owner, repo)}/resolve/main/file-list.json`;
    const res = await fetch(url, { cache: 'no-cache' });
    if (!res.ok) throw new Error('file-list unavailable');
    const json = await res.json();
    // Expected: { files: ["a.parquet", ...], date: "2026-05-02" }
    if (!Array.isArray(json.files)) throw new Error('Invalid file-list format');
    return json;
  });
}

export async function fetchFileRaw(owner, repo, path) {
  const url = `${CDN(owner, repo)}/resolve/main/${encodeURIComponent(path)}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch ${path}`);
  return res;
}

export async function fetchFileText(owner, repo, path) {
  const r = await fetchFileRaw(owner, repo, path);
  return r.text();
}
