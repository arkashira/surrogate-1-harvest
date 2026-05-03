# vanguard / frontend

## Final synthesized implementation

**Diagnosis (merged, contradictions resolved)**  
- No dev/build pipeline and no entrypoint prevent frontend iteration and production deployment.  
- No asset bundler means no module resolution, no HMR, and manual reloads for every change.  
- Missing static manifest forces runtime Hugging Face API calls from the browser (429/quota risk).  
- No CDN-bypass strategy embedded; public dataset files can and should be fetched via CDN without Authorization.  
- No dev server or production static build command eliminates fast feedback and deployability.

**Chosen approach**  
Create a minimal, production-capable frontend scaffold using Vite (fast HMR + static build). Add a pre-computed `datasets.json` manifest so the UI can construct CDN URLs directly (bypassing HF API). Keep scope to new frontend files only; do not modify backend.

**Concrete files to create** (run in order)

```bash
cd /opt/axentx/vanguard
```

### package.json
```json
{
  "name": "vanguard-frontend",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "devDependencies": {
    "vite": "^5.2.0"
  }
}
```

### vite.config.js
```js
import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  server: {
    port: 5173,
    open: true
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true
  }
});
```

### index.html
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Frontend</title>
  <link rel="stylesheet" href="/src/styles.css" />
</head>
<body>
  <div id="app">
    <header>
      <h1>Vanguard</h1>
      <p class="subtitle">CDN-bypass dataset preview (no HF API from browser)</p>
    </header>

    <main>
      <section class="controls">
        <button id="loadManifest" type="button">Load dataset manifest</button>
        <span id="status" class="muted">Idle</span>
      </section>

      <section id="files" class="file-grid" aria-live="polite"></section>

      <section class="preview">
        <h3>Preview</h3>
        <pre id="previewContent" class="code">No file selected.</pre>
      </section>
    </main>

    <footer>
      <small>Manifest-driven CDN fetches — public dataset files only</small>
    </footer>
  </div>

  <script type="module" src="/src/main.js"></script>
</body>
</html>
```

### src/styles.css
```css
:root {
  --bg: #0f1724;
  --card: #16202e;
  --muted: #94a3b8;
  --accent: #38bdf8;
  --text: #e2e8f0;
  --border: #1e293b;
  --radius: 8px;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}

#app {
  max-width: 980px;
  margin: 0 auto;
  padding: 24px;
}

header h1 { margin: 0 0 4px 0; font-size: 20px; }
.subtitle { margin: 0 0 16px 0; color: var(--muted); font-size: 13px; }

.controls {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-bottom: 16px;
}

button {
  background: var(--accent);
  color: #0f1724;
  border: none;
  padding: 8px 14px;
  border-radius: 6px;
  cursor: pointer;
  font-weight: 600;
}

button:disabled { opacity: 0.5; cursor: not-allowed; }

.muted { color: var(--muted); font-size: 13px; }

.file-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 8px;
  margin-bottom: 16px;
}

.file-card {
  background: var(--card);
  border: 1px solid var(--border);
  padding: 10px 12px;
  border-radius: var(--radius);
  cursor: pointer;
  font-size: 13px;
  transition: border-color 0.15s;
}

.file-card:hover { border-color: var(--accent); }

.file-card .path { font-weight: 600; color: var(--text); }
.file-card .size { color: var(--muted); font-size: 12px; margin-top: 4px; }

.preview {
  background: var(--card);
  border: 1px solid var(--border);
  padding: 12px;
  border-radius: var(--radius);
}

.code {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  color: #cbd5e1;
  max-height: 320px;
  overflow: auto;
}
```

### src/main.js
```js
const manifestUrl = '/manifest/datasets.json';
const statusEl = document.getElementById('status');
const filesEl = document.getElementById('files');
const previewEl = document.getElementById('previewContent');
const loadBtn = document.getElementById('loadManifest');

function setStatus(msg, type = 'muted') {
  statusEl.textContent = msg;
  statusEl.className = type === 'error' ? '' : 'muted';
  statusEl.style.color = type === 'error' ? '#f87171' : '';
}

function humanSize(bytes) {
  if (!bytes && bytes !== 0) return '-';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let n = Number(bytes);
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(1)} ${units[i]}`;
}

// Construct public HuggingFace CDN URL for a dataset file.
// Example repo: "datasets/myorg/surrogate-1"
// Example file: "batches/mirror-merged/2026-04-29/a.parquet"
function cdnUrl(repo, filePath, revision = 'main') {
  const encodedRepo = encodeURIComponent(repo);
  const encodedPath = encodeURIComponent(filePath);
  return `https://huggingface.co/datasets/${encodedRepo}/resolve/${revision}/${encodedPath}`;
}

async function fetchManifest() {
  try {
    setStatus('Loading manifest...');
    const res = await fetch(manifestUrl, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Failed to load manifest: ${res.status}`);
    const data = await res.json();
    return data;
  } catch (err) {
    setStatus(String(err), 'error');
    throw err;
  }
}

function renderFiles(entries) {
  filesEl.innerHTML = '';
  if (!Array.isArray(entries) || entries.length === 0) {
    filesEl.innerHTML = '<p class="
