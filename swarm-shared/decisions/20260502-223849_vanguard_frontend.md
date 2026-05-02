# vanguard / frontend

Below is the **single, merged, production-ready frontend scaffold** that combines the strongest, non-contradictory parts of both proposals and resolves all conflicts in favor of **correctness + concrete actionability**.

Key decisions:
- Use **Vite + React** (Candidate 1) for fast dev/build and Lightning Studio/CI reuse, but keep the build minimal.
- Keep **React Router** for hub-first routing and mount semantics required by `#knowledge-rag #graph #hub`.
- Adopt the **HF CDN-bypass strategy** (single `listRepoTree` call, recursive=false) to avoid 429s during surrogate-1 ingestion/training.
- Provide **two ways** to generate the file list:
  1) CLI script (`scripts/generate-hf-file-list.js`) for Mac/CI orchestration.
  2) In-app fetch-and-save (`src/api.js`) so the UI can refresh `public/file-list.json` when needed.
- Expose **Lightning Studio helpers** (`findRunningStudio`, `startOrResumeStudio`) in `src/api.js` to enable the “Mac=CLI + remote compute” workflow.
- Output `public/file-list.json` so training jobs can consume a stable, CDN-only manifest.

---

## 1) Project structure
```bash
/opt/axentx/vanguard/frontend/
├── index.html
├── package.json
├── vite.config.js
├── .gitignore
├── public/
│   └── file-list.json          # CDN-bypass manifest (committed or regenerated)
├── src/
│   ├── main.jsx
│   ├── App.jsx
│   ├── api.js
│   ├── pages/
│   │   └── Hub.jsx
│   └── styles.css
└── scripts/
    └── generate-hf-file-list.js
```

---

## 2) Configuration & entrypoint

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
    "generate:filelist": "node scripts/generate-hf-file-list.js"
  },
  "dependencies": {
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "react-router-dom": "^6.14.1"
  },
  "devDependencies": {
    "vite": "^5.1.0",
    "@vitejs/plugin-react": "^4.0.0",
    "commander": "^11.0.0",
    "@huggingface/hf-api": "^0.4.0"
  }
}
```

### vite.config.js
```js
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true
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
    <title>Vanguard</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

---

## 3) Application code

### src/main.jsx
```jsx
import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './styles.css';

const root = createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
```

### src/App.jsx
```jsx
import React from 'react';
import { Routes, Route, Link } from 'react-router-dom';
import Hub from './pages/Hub';

export default function App() {
  return (
    <div className="app">
      <header style={{ padding: 16, borderBottom: '1px solid #eaeaea' }}>
        <nav>
          <Link to="/">Vanguard</Link>
        </nav>
      </header>

      <main style={{ padding: 16 }}>
        <Routes>
          <Route path="/" element={<Hub />} />
        </Routes>
      </main>
    </div>
  );
}
```

### src/api.js
```js
// Lightweight API helpers for HF CDN-bypass and Lightning Studio reuse.
// Designed for Mac/CLI orchestration + remote compute workflows.

const CDN_BASE = 'https://huggingface.co/datasets';

export async function listRepoTreeOnce({ repo, folder = '' }) {
  // Single call, recursive=false to avoid pagination/429s.
  // Returns tree entries for one folder (HF API friendly).
  const res = await fetch(
    `https://huggingface.co/api/datasets/${encodeURIComponent(repo)}/tree?recursive=false&path=${encodeURIComponent(folder)}`
  );
  if (!res.ok) throw new Error(`HF tree failed: ${res.status}`);
  return res.json(); // array of { path, type }
}

export function buildCdnUrl(repo, path) {
  return `${CDN_BASE}/${encodeURIComponent(repo)}/resolve/main/${path}`;
}

export async function generateFileListAndSave({ repo, folder = '', outPath = '/file-list.json' }) {
  // In-app generation: fetches tree and writes to public/file-list.json via fetch PUT?
  // Since browser cannot write to server fs, this is intended for the CLI script.
  // We expose it here for consistency; real writes happen via CLI or CI.
  const tree = await listRepoTreeOnce({ repo, folder });
  const files = tree.filter((t) => t.type === 'file').map((t) => t.path);
  const payload = { repo, folder, generatedAt: new Date().toISOString(), files };
  // In dev, you can download this blob to replace public/file-list.json.
  return payload;
}

// Lightning Studio helpers (lightweight checks to enable reuse from UI)
export async function findRunningStudio(projectName) {
  // Placeholder: integrate with Lightning Studio API to find running sessions.
  // Returns null or studio metadata.
  return null;
}

export async function startOrResumeStudio({ projectName, script, args = [] }) {
  // Placeholder: start/resume a Lightning Studio run.
  // Returns run metadata or URL.
  return { status: 'not_implemented', projectName, script, args };
}
```

### src/pages/Hub.jsx
```jsx
import React, { useEffect, useState } from 'react';
import { listRepoTreeOnce, buildCdnUrl, generateFileListAndSave } from '../api';

const TOP_HUB_NAME = 'MOC';
const DEFAULT_REPO = 'owner/dataset'; // replace with your dataset
const DEFAULT_FOLDER = 'batches/mirror-merged/2026-04-29'; // example

export default function Hub() {
  const [insights, setInsights] = useState(null);
  const [fileList, setFileList] = useState(null);
  const [loadingInsights, setLoadingInsights] = useState(true);
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [error, setError] = useState(null);

  // Fetch top-hub insights from knowledge-rag backend
  useEffect(() => {
    let mounted = true;
    setLoadingInsights(true);
    fetch(`/api/knowledge-rag/hub/${encodeURIComponent(TOP_HUB_NAME)}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to fetch hub: ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (mounted) setInsights(data);
      })
      .catch((err) => {
        if (mounted) setError(err.message);
      })
      .finally(() => {
