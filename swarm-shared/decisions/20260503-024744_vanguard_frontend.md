# vanguard / frontend

# Final Synthesis — Minimal Vite Frontend for Vanguard (CDN-first)

## 1. Diagnosis (merged)
- No frontend entrypoint or mount point → cannot render or iterate locally.
- No build/dev tooling → no HMR, no deterministic asset hashing, no type-safe imports; every change requires manual refresh.
- No static file manifest embedded → every session re-enumerates HF repos at runtime, burning quota and risking 429s.
- No CDN-only fetch strategy wired into the client → training/inference assets still rely on `/api/` calls instead of `resolve/main/` bypass.
- Missing lightweight session cache for file-list → repeated runs re-query HF instead of reusing embedded manifest.

## 2. Single concrete plan
Add a minimal Vite-based frontend scaffold (vanilla JS) under `/opt/axentx/vanguard/` that:
- Provides `index.html` mount and deterministic dev/prod builds.
- Embeds a build-time file-list manifest (`__FILE_LIST__`) generated once by a script.
- Uses HF CDN (`resolve/main/...`) for direct asset fetches (no auth, no API quota).
- Falls back to runtime tree fetch only when explicitly requested (button) and only against public repos.
- Avoids frameworks and backend changes; ships in <2 hours.

## 3. Implementation (single authoritative set)

```bash
cd /opt/axentx/vanguard
```

### 3.1 Project + Vite

```bash
npm init -y
npm install vite --save-dev
```

`vite.config.js`
```js
import { defineConfig } from 'vite';
import fs from 'fs';
import path from 'path';

function embedFileList() {
  const listPath = path.resolve('scripts/file-list.json');
  if (fs.existsSync(listPath)) {
    return JSON.parse(fs.readFileSync(listPath, 'utf8'));
  }
  return [];
}

export default defineConfig({
  define: {
    __FILE_LIST__: JSON.stringify(embedFileList()),
  },
  build: {
    manifest: true,
    rollupOptions: {
      output: {
        assetFileNames: 'assets/[name]-[hash][extname]',
        chunkFileNames: 'assets/[name]-[hash].js',
      },
    },
  },
  server: {
    port: 5173,
    open: true,
  },
});
```

### 3.2 Entrypoint

`index.html`
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard</title>
  <link rel="stylesheet" href="/src/styles.css" />
</head>
<body>
  <div id="app"></div>
  <script type="module" src="/src/main.js"></script>
</body>
</html>
```

### 3.3 Source (vanilla)

`src/main.js`
```js
import App from './App.js';

const mount = document.getElementById('app');
if (!mount) throw new Error('Mount #app missing');
App(mount);
```

`src/App.js`
```js
function createEl(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    el.append(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return el;
}

function formatSize(bytes) {
  if (bytes == null) return '?';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderFileList(container, files) {
  container.innerHTML = '';
  if (!files.length) {
    container.append(createEl('p', {}, 'No files in manifest. Run scripts/gen-file-list.js to generate one.'));
    return;
  }
  const ul = createEl('ul', { class: 'file-list' });
  for (const f of files) {
    // CDN-first: direct resolve link (no Authorization required for public repos)
    const cdnUrl = `https://huggingface.co/datasets/${f.repo || 'datasets'}/resolve/main/${f.path}`;
    ul.append(
      createEl('li', {},
        createEl('a', { href: cdnUrl, target: '_blank', rel: 'noopener' }, f.path),
        ` — ${formatSize(f.size)}`
      )
    );
  }
  container.append(ul);
}

export default function App(mount) {
  const files = window.__FILE_LIST__ || [];

  const title = createEl('h1', {}, 'Vanguard — CDN-first file browser');
  const desc = createEl('p', {}, 'Build-time embedded manifest + direct HF CDN fetches (resolve/main).');
  const listContainer = createEl('div', { class: 'list' });
  const refreshBtn = createEl('button', { class: 'refresh' }, 'Runtime tree fetch (public repo)');

  renderFileList(listContainer, files);

  refreshBtn.addEventListener('click', async () => {
    refreshBtn.disabled = true;
    refreshBtn.textContent = 'Loading...';
    try {
      // Example: public repo/folder. Adjust as needed.
      const repo = 'datasets';
      const folder = 'some/date-folder';
      const res = await fetch(
        `https://huggingface.co/api/datasets/${repo}/tree?path=${encodeURIComponent(folder)}&recursive=false`,
        { headers: {} } // no auth for public repos
      );
      if (!res.ok) throw new Error(`HF tree API failed: ${res.status}`);
      const tree = await res.json();
      const runtimeFiles = tree
        .filter((t) => t.type === 'file')
        .map((t) => ({
          repo,
          path: `${folder}/${t.path || t.name}`.replace(/\/\/+/g, '/'),
          size: t.size,
          type: t.type,
        }));
      renderFileList(listContainer, runtimeFiles);
    } catch (err) {
      listContainer.innerHTML = '';
      listContainer.append(createEl('p', { style: 'color:red' }, String(err)));
    } finally {
      refreshBtn.disabled = false;
      refreshBtn.textContent = 'Runtime tree fetch (public repo)';
    }
  });

  mount.innerHTML = '';
  mount.append(
    createEl('div', { class: 'container' }, title, desc, refreshBtn, listContainer)
  );
}
```

`src/styles.css`
```css
body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial; margin: 2rem; color: #111; }
.file-list { list-style: none; padding: 0; }
.file-list li { margin: 0.25rem 0; word-break: break-all; }
button { padding: 0.5rem 1rem; cursor: pointer; }
.container { max-width: 900px; }
```

### 3.4 Manifest generator (run on Mac or any env)

`scripts/gen-file-list.js`
```js
#!/usr/bin/env node
// Usage: HF_TOKEN=... node scripts/gen-file-list.js <repo> <folder>
// Produces scripts/file-list.json for build-time embedding.
import { writeFileSync, mkdirSync } from 'fs';
import { resolve } from 'path';
import { fileURLToPath } from 'url';

const __dirname = resolve(fileURLToPath(import.meta.url), '..');

async function main() {
  const repo = process.argv[2] || 'datasets';
  const folder = process.argv[3] || 'some/date-folder';
  const token = process.env.HF_TOKEN || ''
