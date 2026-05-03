# vanguard / frontend

## Final synthesized plan (correctness + concrete actionability)

**Core diagnosis (accepted from both candidates)**
- No frontend entrypoint or build pipeline → no fast feedback loop for UI changes.
- Frontend must not make runtime HF API calls from the browser (rate-limit/429 risk, quota burn).
- No embedded manifest → every session re-enumerates repos; no deterministic CDN-only file selection.

**Single, minimal, production-ready change**
Add a static frontend entrypoint + asset pipeline that:
- Embeds a pre-computed manifest at build time so the UI can perform CDN-only fetches (no Authorization header, no HF API).
- Uses esbuild for fast bundling and a dev server for local iteration.
- Exposes one deterministic render function and a safe CDN fetch helper.
- Serves `dist/` as static assets; keeps CSP tight.

**File set to create (concrete paths)**
- `/opt/axentx/vanguard/public/index.html`
- `/opt/axentx/vanguard/src/main.js`
- `/opt/axentx/vanguard/src/App.js`
- `/opt/axentx/vanguard/src/style.css`
- `/opt/axentx/vanguard/manifest.json` (sample; ops will generate/overwrite in CI)
- `/opt/axentx/vanguard/package.json`
- `/opt/axentx/vanguard/build.sh`
- `/opt/axentx/vanguard/dev.sh` (optional dev server)

---

### 1) package.json (tooling)
```json
{
  "name": "vanguard-frontend",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "scripts": {
    "build": "bash build.sh",
    "dev": "bash dev.sh"
  },
  "devDependencies": {
    "esbuild": "^0.21.5"
  }
}
```

---

### 2) public/index.html (CSP-safe mount)
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard</title>
  <meta http-equiv="Content-Security-Policy" content="
    default-src 'self';
    script-src 'self';
    style-src 'self' 'unsafe-inline';
    img-src 'self' data: https:;
    connect-src 'self' https://huggingface.co;
  ">
  <link rel="stylesheet" href="/style.css" />
</head>
<body>
  <div id="app">Loading…</div>
  <script type="module" src="/main.js"></script>
</body>
</html>
```

---

### 3) manifest.json (sample; ops will generate)
```json
{
  "generatedAt": "2026-05-03T03:00:00Z",
  "datasets": [
    {
      "repo": "datasets/example-a",
      "folder": "2026-05-01",
      "files": ["part-00000.parquet", "_SUCCESS"]
    },
    {
      "repo": "datasets/example-b",
      "folder": "2026-05-02",
      "files": ["part-00000.parquet"]
    }
  ]
}
```

---

### 4) src/style.css
```css
:root {
  --bg: #0f172a;
  --card: #1e293b;
  --accent: #38bdf8;
  --text: #e2e8f0;
  --muted: #94a3b8;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  -webkit-font-smoothing: antialiased;
}

.app-root {
  max-width: 900px;
  margin: 2rem auto;
  padding: 1rem;
}

header {
  margin-bottom: 1rem;
}

.dataset-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  gap: 0.75rem;
}

.dataset-item {
  background: var(--card);
  padding: 1rem;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.04);
}

.files {
  margin-top: 0.5rem;
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

code {
  background: rgba(0,0,0,0.3);
  padding: 0.15rem 0.4rem;
  border-radius: 4px;
  font-size: 0.9em;
}

.actions {
  margin-top: 0.5rem;
}

button {
  background: var(--accent);
  color: #0f172a;
  border: none;
  padding: 0.4rem 0.75rem;
  border-radius: 6px;
  cursor: pointer;
  font-weight: 600;
}

button:hover { opacity: 0.9; }

.status {
  margin-top: 0.5rem;
  color: var(--muted);
  font-size: 0.9em;
}
```

---

### 5) src/App.js (deterministic render + CDN-only fetch)
```javascript
// Deterministic app renderer. Never calls HF API from the browser.
// Uses CDN-only fetch (no Authorization header) for public assets.

export function createApp({ manifest, cdnFetch }) {
  const root = document.createElement('div');
  root.className = 'app-root';

  const header = document.createElement('header');
  header.innerHTML = `
    <h1>Vanguard</h1>
    <small>Manifest: ${manifest.generatedAt}</small>
  `;
  root.appendChild(header);

  const list = document.createElement('ul');
  list.className = 'dataset-list';

  manifest.datasets.forEach((ds) => {
    const li = document.createElement('li');
    li.className = 'dataset-item';
    li.innerHTML = `
      <strong>${ds.repo}</strong> / ${ds.folder}
      <div class="files">
        ${ds.files.map((f) => `<code>${f}</code>`).join('')}
      </div>
      <div class="actions">
        <button class="preview-btn" data-repo="${ds.repo}" data-folder="${ds.folder}">Preview CDN</button>
      </div>
      <div class="status" aria-live="polite"></div>
    `;
    list.appendChild(li);
  });

  root.appendChild(list);

  // Lightweight preview: HEAD first file via CDN (no HF API).
  list.addEventListener('click', async (e) => {
    const btn = e.target.closest('.preview-btn');
    if (!btn) return;
    const repo = btn.dataset.repo;
    const folder = btn.dataset.folder;
    const ds = manifest.datasets.find((d) => d.repo === repo && d.folder === folder);
    if (!ds || !ds.files.length) return;

    const status = btn.closest('.dataset-item').querySelector('.status');
    const first = ds.files[0];
    status.textContent = 'Checking CDN…';

    try {
      // CDN fetch (no Authorization header) — avoids browser-side HF API quota/429s.
      const res = await cdnFetch(repo, `${folder}/${first}`, { method: 'HEAD' });
      const size = res.headers.get('content-length') || 'unknown';
      status.textContent = `CDN OK — ${size} bytes — ${first}`;
    } catch (err) {
      status.textContent = `CDN error: ${err.message}`;
    }
  });

  return root;
}

// Default CDN path builder
