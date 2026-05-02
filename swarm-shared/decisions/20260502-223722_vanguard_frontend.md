# vanguard / frontend

## Final consolidated implementation (strongest + correct + actionable)

**Summary of decisions**
- Keep Candidate 1’s file structure and route semantics (`/`, `/ingest`, `/train`) — they’re clearer than Candidate 2’s single-app.js approach.
- Use Candidate 1’s `index.html` + `src/main.js` + `src/api.js` + `styles.css` layout (idempotent, minimal, and immediately deployable).
- Fix the truncated `api.js` and make the Lightning Studio guard realistic and safe:
  - Frontend only orchestrates/starts work via authenticated backend endpoints (or signed fetch to a controlled proxy). It does **not** embed secrets or run heavy SDKs in the browser.
  - Provide a small Node orchestrator (`orchestrator.js`) for the real Lightning Studio reuse guard and surrogate-1 training commands (this is where quota/stop/start logic belongs).
- Add explicit CDN-bypass guidance and a safe file-list ingestion flow (paste JSON + optional server-side cache) to avoid HF 429s during surrogate-1 training.
- Add a tiny `config` surface so repo/path/target can be set without editing code.
- Keep total frontend ~120–150 lines; ship in <2h.

---

### 1) Create project layout

```bash
cd /opt/axentx/vanguard
mkdir -p src
```

---

### 2) `index.html` (canonical SPA entrypoint)

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Knowledge-RAG Hub</title>
  <link rel="stylesheet" href="./styles.css" />
</head>
<body>
  <header class="top-bar">
    <h1>Vanguard</h1>
    <nav aria-label="Main">
      <a href="#/" data-route="/">Hub</a>
      <a href="#/ingest" data-route="/ingest">Ingest</a>
      <a href="#/train" data-route="/train">Train</a>
    </nav>
  </header>

  <main id="app" class="container" role="main">
    <!-- dynamic content -->
  </main>

  <footer class="muted">
    Pattern-driven frontend — #knowledge-rag #graph #hub
  </footer>

  <script type="module" src="./src/main.js"></script>
</body>
</html>
```

---

### 3) `src/config.js` (single source of truth)

```javascript
// Adjust these defaults to your repo/paths. Keep tokens out of frontend in prod.
export const CONFIG = {
  hfRepo: 'axentx/surrogate-1',
  hfPath: 'batches/mirror-merged/2026-05-02',
  studioName: 'vanguard-l40s',
  studioMachine: 'L40S',
  // Backend endpoints for privileged actions (recommended). Leave empty to use direct fetch for public ops only.
  endpoints: {
    listRepo: '',           // e.g., /api/hf/list
    createStudio: '',       // e.g., /api/studio/create
    startTraining: ''       // e.g., /api/train/start
  }
};
```

---

### 4) `src/api.js` (complete + safe)

```javascript
import { CONFIG } from './config.js';

const $ = (sel) => document.querySelector(sel);

function bearer() {
  const t = localStorage.getItem('hf_token') || '';
  return t ? `Bearer ${t}` : undefined;
}

// List a single folder (non-recursive) to stay within HF rate limits.
// Prefer backend proxy for production to hide token and add caching.
export async function hfFileList({ repo = CONFIG.hfRepo, path = CONFIG.hfPath } = {}) {
  if (!repo) throw new Error('repo required (owner/repo)');
  const apiUrl = `https://huggingface.co/api/models/${repo}/tree/${encodeURIComponent(path)}?recursive=false`;
  const res = await fetch(apiUrl, {
    headers: bearer() ? { Authorization: bearer() } : {}
  });
  if (!res.ok) throw new Error(`HF list failed: ${res.status} ${res.statusText}`);
  return res.json(); // array of { rfilename, type, size, ... }
}

// Best-practice: do not run Lightning SDK in browser.
// These functions hit backend endpoints (if configured) or return safe instructions.
export async function lightningReuseOrCreate({ name = CONFIG.studioName, machine = CONFIG.studioMachine } = {}) {
  const ep = CONFIG.endpoints.createStudio;
  if (ep) {
    const r = await fetch(ep, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, machine })
    });
    if (!r.ok) throw new Error(`Studio request failed: ${r.status}`);
    return r.json();
  }
  // Fallback: return guidance for local orchestrator usage.
  return {
    note: 'Lightning actions require backend orchestrator. Run locally:',
    command: `node orchestrator.js studio-reuse --name ${name} --machine ${machine}`
  };
}

export async function startTrainingJob({ files = [], studio = CONFIG.studioName } = {}) {
  const ep = CONFIG.endpoints.startTraining;
  if (ep) {
    const r = await fetch(ep, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ studio, files })
    });
    if (!r.ok) throw new Error(`Start training failed: ${r.status}`);
    return r.json();
  }
  return {
    note: 'Training start requires backend or local orchestrator.',
    command: `node orchestrator.js train --studio ${studio} --files ${JSON.stringify(files)}`
  };
}

// Helpers for frontend-only flows
export function saveFileListToLocal(list) {
  if (!Array.isArray(list)) throw new Error('Expected array of file paths');
  localStorage.setItem('vanguard:hfFileList', JSON.stringify(list));
  return list.length;
}

export function loadFileListFromLocal() {
  const raw = localStorage.getItem('vanguard:hfFileList');
  return raw ? JSON.parse(raw) : null;
}
```

---

### 5) `src/main.js` (complete router + actions)

```javascript
import { hfFileList, lightningReuseOrCreate, startTrainingJob, saveFileListToLocal, loadFileListFromLocal } from './api.js';

const $ = (sel) => document.querySelector(sel);

function out(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function renderHub() {
  $('#app').innerHTML = `
    <section class="card">
      <h2>Top Hub — MOC (2026-04-27)</h2>
      <p class="muted">Review the most-connected hub before planning tasks. Use contextual insights to guide ingestion and surrogate-1 training.</p>
      <div class="actions">
        <button id="load-file-list">Load HF File List (CDN-bypass)</button>
        <button id="reuse-studio">Reuse Lightning Studio (L40S)</button>
        <button id="start-training">Start Training (preview)</button>
      </div>
      <pre id="output" class="output" aria-live="polite"></pre>
    </section>
  `;

  $('#load-file-list').addEventListener('click', async () => {
    out('#output', 'Fetching repo tree (single API call)...');
    try {
      const list = await hfFileList({});
      saveFileListToLocal(list.map((f) => f.rfilename || f.path || f));
      out('#output', JSON.stringify(list, null, 2));
    } catch (err) {
      out('#output', `Error: ${err.message}`);
    }
  });

  $('#reuse-studio').addEventListener('click', async () => {
    out('#output', 'Checking Lightning studios...');
    try {
      const studio = await lightningReuseOrCreate({});
      out('#output', JSON.stringify(studio, null, 2));
    } catch (err)
