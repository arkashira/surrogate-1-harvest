# vanguard / quality

## Final synthesized plan (correct + actionable)

**Core problem**: the project has no deterministic frontend build, no mount point, no manifest, and no CDN-first strategy — leading to full reloads, brittle asset paths, hardcoded/guessed URLs, and accidental HF API calls that risk rate limits.

**Goal (scope-constrained)**: add a minimal Vite-based frontend scaffold and a manifest generator that runs on the Mac orchestration host. Do not modify backend/training code.

---

## 1) Repository changes (add only)

- `package.json` — dev tooling and deterministic scripts (`dev`, `build`, `preview`, `manifest`)
- `vite.config.js` — CSP-friendly defaults, `base: './'`, deterministic hashed output
- `index.html` — mount point (`<div id="app">`) and strict CSP meta
- `src/main.js` + `src/App.js` — small, framework-free UI that:
  - reads `?date=` from URL
  - loads `/manifest.json` (generated file)
  - renders CDN links (`resolve/main/...`) so no HF API calls happen in the browser
- `public/manifest.json` — placeholder committed; generated file replaces/overwrites it during CI/dev
- `scripts/generate-manifest.js` — run on Mac host; lists one date folder via HF API with exponential backoff and 360s wait on 429; emits CDN-only `manifest.json`
- `.gitignore` additions — `node_modules/`, `dist/`

---

## 2) Key resolved decisions (correctness + actionability)

- **Use Vite (ESM)** — gives HMR, deterministic hashing, and a reproducible dev server (`npm run dev`). No bundler changes to backend code.
- **CDN-first only in UI** — frontend never calls `load_dataset` or HF data API. It uses `https://huggingface.co/datasets/<repo>/resolve/main/<path>` URLs from the manifest.
- **Single manifest file at `/manifest.json`** — simplest contract for the frontend. Generator writes here; frontend fetches it. Avoids extra routing complexity.
- **Generator runs on Mac host** — follows “Mac=CLI rule” (orchestration only). It uses HF API once per date folder, backs off on 429 (360s), and produces the file for local dev/CI.
- **CSP and security** — strict but functional defaults in `index.html` and Vite config to avoid common issues while allowing CDN assets.
- **No changes to training/backend** — scope respected.

---

## 3) Implementation (copy/paste ready)

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
    "preview": "vite preview",
    "manifest": "node scripts/generate-manifest.js"
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
  publicDir: 'public',
  base: './',
  server: {
    port: 5173,
    strictPort: true,
    host: true
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: true,
    rollupOptions: {
      output: {
        assetFileNames: 'assets/[name]-[hash][extname]',
        chunkFileNames: 'assets/[name]-[hash].js',
        entryFileNames: 'assets/[name]-[hash].js'
      }
    }
  }
});
```

### index.html
```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1.0" />
    <meta http-equiv="Content-Security-Policy" content="default-src 'self' data: https://huggingface.co https://huggingface.co/datasets; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' https://huggingface.co https://huggingface.co/datasets;">
    <title>Vanguard — Dataset Viewer</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.js"></script>
  </body>
</html>
```

### src/main.js
```js
import App from './App.js';

const mount = document.getElementById('app');
if (!mount) throw new Error('Mount point #app missing');
mount.appendChild(App());
```

### src/App.js
```js
export default function App() {
  const root = document.createElement('div');
  root.innerHTML = `
    <header style="font-family:system-ui,sans-serif;padding:1rem;border-bottom:1px solid #eaeaea">
      <h1 style="margin:0;font-size:1.25rem">Vanguard — CDN-first Dataset Viewer</h1>
    </header>
    <main style="padding:1rem">
      <section>
        <label for="date">Date folder:</label>
        <input id="date" placeholder="e.g. 2026-04-29" style="margin-left:0.5rem;padding:0.25rem" />
        <button id="load" style="margin-left:0.5rem">Load manifest</button>
      </section>
      <section id="status" style="margin-top:0.5rem;color:#666"></section>
      <ul id="files" style="margin-top:0.5rem;padding-left:1.25rem"></ul>
    </main>
  `;

  const dateInput = root.querySelector('#date');
  const loadBtn = root.querySelector('#load');
  const statusEl = root.querySelector('#status');
  const filesEl = root.querySelector('#files');

  async function loadManifest(date) {
    if (!date) {
      statusEl.textContent = 'Enter a date folder (e.g. 2026-04-29)';
      return;
    }
    statusEl.textContent = 'Loading manifest...';
    filesEl.innerHTML = '';
    try {
      const res = await fetch(`/manifest.json?date=${encodeURIComponent(date)}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const manifest = await res.json();
      statusEl.textContent = `Loaded ${manifest.files?.length || 0} files for ${manifest.date || date}`;
      (manifest.files || []).forEach((f) => {
        const li = document.createElement('li');
        const a = document.createElement('a');
        a.href = f.cdnUrl || `https://huggingface.co/datasets/${manifest.repo || 'datasets'}/resolve/main/${f.path}`;
        a.textContent = f.path;
        a.target = '_blank';
        a.rel = 'noopener';
        li.appendChild(a);
        filesEl.appendChild(li);
      });
    } catch (err) {
      statusEl.textContent = `Failed to load manifest: ${err.message}`;
    }
  }

  loadBtn.addEventListener('click', () => loadManifest(dateInput.value.trim()));
  dateInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') loadManifest(dateInput.value.trim());
  });

  const params = new URLSearchParams(window.location.search);
  const q = params.get('date');
  if (q) {
    dateInput.value = q;
    loadManifest(q);
  }

  return root;
}
```

### public/manifest.json (placeholder)
```json
{
  "date": "YYYY-MM-DD",
  "repo": "datasets/owner/repo",
  "note": "Generate with scripts/generate-manifest.js and replace this file (or serve generated file at /manifest.json)",
  "files": []
}
```

### scripts/generate-manifest.js
```js
#!/usr/bin/env node
/**
 * Generate a CDN-only file
