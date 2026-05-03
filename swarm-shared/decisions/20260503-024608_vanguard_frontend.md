# vanguard / frontend

## Final synthesized answer (strongest parts, resolved contradictions)

**Decision**: Use Vite (Candidate 1) for fast feedback and build-time manifest embedding, but keep the scaffold minimal and avoid premature React/Vue. This gives hot reload, deterministic CDN-only fetches, and a <2h ship path while avoiding runtime HF API calls from the browser.

### Why this wins
- Vite gives instant HMR and a real build pipeline (Candidate 1) without heavy config.
- Embedding the manifest at build time (Candidate 1) is safer and more deterministic than runtime generation; it enforces the CDN-only pattern and prevents quota/429 issues.
- Keeping the UI vanilla (Candidate 2’s spirit) avoids framework churn and keeps the change small.
- Candidate 2’s concern about “no tooling” is addressed by adding Vite, but we keep the file count low (~6 files) and the config minimal.

---

### Implementation (run these commands)

```bash
cd /opt/axentx/vanguard
```

#### package.json
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

#### vite.config.js
```js
import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: 'index.html'
      }
    }
  },
  server: {
    port: 5173,
    open: true
  }
});
```

#### index.html
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Dataset Explorer</title>
  <link rel="stylesheet" href="/src/App.css" />
</head>
<body>
  <div id="app"></div>
  <script type="module" src="/src/main.js"></script>
</body>
</html>
```

#### src/main.js
```js
import App from './App.js';

const mount = document.getElementById('app');
if (mount) {
  mount.appendChild(App());
}
```

#### src/App.css
```css
:root {
  --bg: #0b0f19;
  --card: #111827;
  --muted: #6b7280;
  --accent: #22d3ee;
  --text: #f3f4f6;
  --radius: 8px;
}

* { box-sizing: border-box; }
body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial; background: var(--bg); color: var(--text); }
#app { min-height: 100vh; padding: 24px; }
.app-card { background: var(--card); border-radius: var(--radius); padding: 20px; max-width: 900px; margin: 0 auto; }
.header { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:16px; }
.title { font-size:20px; font-weight:600; color:var(--text); margin:0; }
.meta { font-size:13px; color:var(--muted); }
.list { list-style:none; padding:0; margin:0; display:flex; flex-direction:column; gap:8px; }
.item { padding:10px 12px; background: rgba(255,255,255,0.02); border-radius:6px; font-size:13px; color:var(--muted); display:flex; justify-content:space-between; align-items:center; }
.item .path { color:var(--accent); font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, "Roboto Mono", monospace; font-size:12px; }
.badge { font-size:11px; padding:3px 8px; border-radius:999px; background: rgba(34,211,239,0.12); color:var(--accent); }
.empty { color:var(--muted); font-size:13px; }
.spinner { width:16px; height:16px; border-radius:50%; border:2px solid rgba(255,255,255,0.06); border-top-color:var(--accent); animation:spin 1s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
```

#### src/App.js
```js
import manifest from '../static/file-manifest.json?url';

export default function App() {
  const root = document.createElement('div');
  root.className = 'app-card';

  const header = document.createElement('div');
  header.className = 'header';
  const title = document.createElement('h1');
  title.className = 'title';
  title.textContent = 'Vanguard — Dataset Explorer';
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = 'CDN-only fetches (no HF API)';
  header.appendChild(title);
  header.appendChild(meta);

  const listWrap = document.createElement('div');
  const list = document.createElement('ul');
  list.className = 'list';
  const loading = document.createElement('li');
  loading.className = 'item';
  loading.innerHTML = '<span>Loading manifest...</span><span class="spinner"></span>';
  list.appendChild(loading);
  listWrap.appendChild(list);

  root.appendChild(header);
  root.appendChild(listWrap);

  fetch(manifest)
    .then((r) => {
      if (!r.ok) throw new Error('Failed to load manifest');
      return r.json();
    })
    .then((files) => {
      list.innerHTML = '';
      if (!files || !files.length) {
        const el = document.createElement('li');
        el.className = 'item';
        el.innerHTML = '<span class="empty">No files in manifest</span>';
        list.appendChild(el);
        return;
      }
      files.forEach((f) => {
        const li = document.createElement('li');
        li.className = 'item';
        const repo = f.repo || 'datasets/anonymous';
        const cdn = `https://huggingface.co/datasets/${encodeURIComponent(repo)}/resolve/main/${encodeURIComponent(f.path)}`;
        li.innerHTML = `
          <span class="path">${escapeHtml(f.path)}</span>
          <span>
            <a href="${cdn}" target="_blank" rel="noopener noreferrer" style="color:var(--accent);font-size:12px;text-decoration:none;">cdn</a>
            <span class="badge">${escapeHtml(repo.split('/').pop() || repo)}</span>
          </span>
        `;
        list.appendChild(li);
      });
    })
    .catch((err) => {
      list.innerHTML = '';
      const el = document.createElement('li');
      el.className = 'item';
      el.innerHTML = `<span class="empty">Error: ${escapeHtml(String(err.message))}</span>`;
      list.appendChild(el);
    });

  return root;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
```

#### static/file-manifest.json
```json
[
  {
    "repo": "datasets/example-repo",
    "path": "data/train.parquet"
  },
  {
    "repo": "datasets/example-repo",
    "path": "data/validation.parquet"
  },
  {
    "repo": "datasets/example-repo",
    "path": "README.md
