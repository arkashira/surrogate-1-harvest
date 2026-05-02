# vanguard / frontend

## 1. Diagnosis

- No canonical frontend entrypoint or routing scaffold — pages are ad-hoc, violating `#knowledge-rag #graph #hub` pattern (no clear “top-hub” view).
- Missing HF CDN-bypass file-list UI/flow — surrogate-1 training will hit 429s when frontend triggers ingestion/training without pre-listed file manifests.
- No Lightning Studio reuse guard — frontend can accidentally spawn new studios and burn quota instead of reusing running ones.
- No structured logging/telemetry surface in frontend to correlate with backend decisions (20260502 logs) and detect idle-stop/retry needs.
- No idempotent cron-safe wrapper for frontend-triggered workflows (wrapper exec errors pattern: missing shebang/permissions/cron SHELL).

## 2. Proposed change

Add a lightweight frontend orchestration layer under `/opt/axentx/vanguard/src/frontend/` (create if absent):

- `src/frontend/index.html` — single-page shell with top-hub view and controls.
- `src/frontend/app.js` — orchestration module exposing:
  - `listHFDateFolder(datePath)` → fetches repo tree once, saves `file-list.json`, returns CDN URLs.
  - `getOrCreateStudio(name, machine)` → reuses running studio or starts one (idempotent).
  - `triggerTraining(fileListUrl)` — uses CDN-only URLs (zero API calls during training).
  - `wrapCronSafe(fn)` — ensures cron-safe invocation (sets SHELL, uses bash, exits cleanly).
- `src/frontend/style.css` — minimal layout.
- `src/frontend/cron-wrapper.sh` — cron-safe wrapper with shebang, executable bit, SHELL export.

Scope: new files only; no existing code modified.

## 3. Implementation

Create directory and files:

```bash
mkdir -p /opt/axentx/vanguard/src/frontend
cd /opt/axentx/vanguard/src/frontend
```

### index.html

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Vanguard — Frontend Orchestrator</title>
  <link rel="stylesheet" href="./style.css" />
</head>
<body>
  <header>
    <h1>Top Hub: MOC</h1>
    <p class="subtitle">Knowledge-RAG graph entrypoint — HF CDN-bypass + Lightning reuse</p>
  </header>

  <main>
    <section id="hf-section">
      <h2>1. List HF date folder (CDN-bypass)</h2>
      <input id="hf-repo" placeholder="datasets/owner/repo" value="datasets/example/data" />
      <input id="hf-date" placeholder="YYYY-MM-DD" value="2026-04-29" />
      <button onclick="handleListHF()">List & Save file-list.json</button>
      <pre id="hf-output"></pre>
    </section>

    <section id="studio-section">
      <h2>2. Lightning Studio (reuse/create)</h2>
      <input id="studio-name" placeholder="vanguard-train-run" value="vanguard-train-run" />
      <select id="studio-machine">
        <option>l40s</option>
        <option>h200</option>
      </select>
      <button onclick="handleStudio()">Get or Start Studio</button>
      <pre id="studio-output"></pre>
    </section>

    <section id="train-section">
      <h2>3. Trigger training (CDN-only)</h2>
      <input id="file-list-url" placeholder="file-list.json URL or path" />
      <button onclick="handleTrain()">Run Training</button>
      <pre id="train-output"></pre>
    </section>

    <section id="cron-section">
      <h2>4. Cron-safe wrapper</h2>
      <button onclick="handleCronTest()">Run cron-wrapper.sh (test)</button>
      <pre id="cron-output"></pre>
    </section>
  </main>

  <script src="./app.js"></script>
</body>
</html>
```

### app.js

```javascript
// Minimal orchestration helpers for frontend workflows.
// Uses HF CDN bypass and Lightning SDK patterns (Lightning calls are示意;
// real credentials/secrets should be handled by backend or secure tokens).

const API_BASE = '/api'; // proxy to backend if needed

function logOutput(id, text) {
  const el = document.getElementById(id);
  el.textContent = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
}

async function handleListHF() {
  const repo = document.getElementById('hf-repo').value.trim();
  const date = document.getElementById('hf-date').value.trim();
  const outputId = 'hf-output';

  // repo examples: "datasets/owner/repo"
  if (!repo || !date) return logOutput(outputId, 'repo and date required');

  try {
    // Ask backend (or direct if allowed) to list tree non-recursively for the date folder.
    // This single API call should be done after rate-limit window clears (pattern).
    const res = await fetch(`${API_BASE}/hf/tree`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, path: date, recursive: false })
    });
    if (!res.ok) throw new Error(`Tree API failed: ${res.status}`);
    const tree = await res.json();

    // Build CDN-only URLs (bypass /api/ auth checks)
    const cdnUrls = (tree.files || []).map(f =>
      `https://huggingface.co/datasets/${repo}/resolve/main/${date}/${f}`
    );
    const manifest = { repo, date, files: tree.files || [], cdnUrls, ts: Date.now() };

    // Persist via backend or localStorage for demo
    localStorage.setItem('file-list-latest', JSON.stringify(manifest));
    logOutput(outputId, manifest);
  } catch (err) {
    logOutput(outputId, `Error: ${err.message}`);
  }
}

async function handleStudio() {
  const name = document.getElementById('studio-name').value.trim();
  const machine = document.getElementById('studio-machine').value;
  const outputId = 'studio-output';

  try {
    // Idempotent studio reuse (pattern: list running and reuse)
    const res = await fetch(`${API_BASE}/lightning/studios`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'get_or_create', name, machine })
    });
    if (!res.ok) throw new Error(`Studio API failed: ${res.status}`);
    const studio = await res.json();
    logOutput(outputId, studio);
  } catch (err) {
    logOutput(outputId, `Error: ${err.message}`);
  }
}

async function handleTrain() {
  const fileListUrl = document.getElementById('file-list-url').value.trim();
  const outputId = 'train-output';

  if (!fileListUrl) {
    // fallback to latest saved manifest
    const saved = localStorage.getItem('file-list-latest');
    if (!saved) return logOutput(outputId, 'No file-list URL and no saved manifest');
    const m = JSON.parse(saved);
    fileListUrl = `file-list-latest`; // placeholder; real usage would pass CDN URLs
  }

  try {
    // Trigger training using CDN-only URLs (zero HF API calls during data loading)
    const res = await fetch(`${API_BASE}/training/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fileListUrl, strategy: 'cdn_only' })
    });
    if (!res.ok) throw new Error(`Training start failed: ${res.status}`);
    const result = await res.json();
    logOutput(outputId, result);
  } catch (err) {
    logOutput(outputId, `Error: ${err.message}`);
  }
}

async function handleCronTest() {
  const outputId = 'cron-output';
  try {
    const res = await fetch(`${API_BASE}/cron/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wrapper: '
