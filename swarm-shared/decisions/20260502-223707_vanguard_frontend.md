# vanguard / frontend

**Final synthesized implementation (combines strongest parts, resolves contradictions, prioritizes correctness + concrete actionability)**

## 1. Diagnosis (consensus)
- No canonical frontend entrypoint or SPA mount → violates `#knowledge-rag #graph #hub` pattern and slows onboarding.
- No top-hub (MOC) landing view to surface contextual insights and graph entrypoints.
- No HF CDN-bypass file-list UI/flow → risks 429s during surrogate-1 ingestion because frontend cannot pre-list and embed a file manifest.
- No lightweight orchestration UI to trigger HF listing → embed manifest → launch Lightning Studio reuse flow.
- Missing routing scaffold and state bootstrap for future graph/context features.

## 2. Proposed change (merged scope)
Create a minimal, production-ready SPA entrypoint and top-hub view at `/opt/axentx/vanguard/` (not nested under `frontend/`), with:
- Canonical `index.html` SPA mount + minimal routing scaffold.
- `src/main.js` for router + bootstrap.
- `src/views/TopHub.js` MOC-centric hub view with contextual insight area and HF file-list orchestration UI.
- `src/services/hf-cdn.js` HF CDN-bypass file-list fetcher (single API call → CDN-only manifest) and Lightning Studio reuse helper.
- `styles.css` for immediate usability.
- Optional `package.json` dev server for local testing (avoids heavy build tooling; keeps scope ~200 lines, ship time <2h).

## 3. Implementation

```bash
cd /opt/axentx/vanguard
mkdir -p src/views src/services
```

### index.html
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard — Top Hub</title>
  <link rel="stylesheet" href="./styles.css" />
</head>
<body>
  <div id="app"></div>
  <script type="module" src="./src/main.js"></script>
</body>
</html>
```

### src/main.js
```js
import { TopHub } from './views/TopHub.js';
import { HFCDN } from './services/hf-cdn.js';

const $app = document.getElementById('app');

function router() {
  const path = window.location.hash.slice(1) || '/';
  if (path === '/' || path === '/hub') {
    $app.innerHTML = '';
    $app.appendChild(TopHub.render());
    TopHub.attach(HFCDN);
    return;
  }
  $app.innerHTML = '<h1>Not found</h1>';
}

window.addEventListener('hashchange', router);
router();
```

### src/views/TopHub.js
```js
export const TopHub = {
  render() {
    const el = document.createElement('div');
    el.className = 'top-hub';
    el.innerHTML = `
      <header class="hub-header">
        <h1>Top Hub — MOC</h1>
        <p class="subtitle">Contextual insights and dataset orchestration (HF CDN-bypass)</p>
      </header>

      <section class="insights">
        <h2>Contextual Insight</h2>
        <div id="insight-body" class="card">
          <em>Review the most-connected hub (MOC) before planning tasks. Use HF CDN-bypass to list once and embed file manifest for surrogate-1 training.</em>
        </div>
      </section>

      <section class="orchestration">
        <h2>HF CDN-bypass File List</h2>
        <div class="form-row">
          <label>
            Dataset repo (e.g. <code>datasets/username/repo</code>)
            <input id="hf-repo" type="text" placeholder="datasets/username/repo" />
          </label>
          <label>
            Path (optional)
            <input id="hf-path" type="text" placeholder="folder/date (leave empty for root)" />
          </label>
          <div class="actions">
            <button id="btn-list">List & Save Manifest</button>
            <button id="btn-launch" disabled>Launch Training (Lightning reuse)</button>
          </div>
        </div>

        <div id="status" class="status" aria-live="polite"></div>

        <div id="manifest-preview" class="preview" hidden>
          <h3>Manifest (embed in train.py)</h3>
          <pre><code id="manifest-json"></code></pre>
        </div>
      </section>

      <footer class="hub-footer">
        <small>Patterns: #knowledge-rag #graph #hub | HF CDN-bypass | Lightning Studio reuse</small>
      </footer>
    `;
    return el;
  },

  attach(HFCDN) {
    const repoInput = document.getElementById('hf-repo');
    const pathInput = document.getElementById('hf-path');
    const btnList = document.getElementById('btn-list');
    const btnLaunch = document.getElementById('btn-launch');
    const status = document.getElementById('status');
    const preview = document.getElementById('manifest-preview');
    const manifestJson = document.getElementById('manifest-json');

    function setStatus(msg, type = 'info') {
      status.textContent = msg;
      status.className = `status ${type}`;
    }

    btnList.onclick = async () => {
      const repo = repoInput.value.trim();
      const path = pathInput.value.trim() || null;
      if (!repo) {
        setStatus('Error: Dataset repo is required.', 'error');
        return;
      }

      setStatus('Listing files (CDN-bypass)...', 'info');
      btnList.disabled = true;
      preview.hidden = true;

      try {
        const result = await HFCDN.listAndSaveManifest({ repo, path });
        setStatus(`Listed ${result.files.length} files. Manifest saved.`, 'success');
        manifestJson.textContent = JSON.stringify(result.manifest, null, 2);
        preview.hidden = false;
        btnLaunch.disabled = false;
        btnLaunch.dataset.manifest = JSON.stringify(result.manifest);
      } catch (err) {
        console.error(err);
        setStatus(`Error: ${err.message || err}`, 'error');
        btnList.disabled = false;
      }
    };

    btnLaunch.onclick = async () => {
      const manifest = btnLaunch.dataset.manifest;
      if (!manifest) {
        setStatus('Error: No manifest available.', 'error');
        return;
      }

      setStatus('Launching Lightning Studio reuse flow...', 'info');
      btnLaunch.disabled = true;

      try {
        const url = HFCDN.buildLightningStudioUrl(JSON.parse(manifest));
        // Open reuse flow in a new tab so user retains this UI
        window.open(url, '_blank', 'noopener,noreferrer');
        setStatus('Lightning Studio reuse flow opened.', 'success');
      } catch (err) {
        console.error(err);
        setStatus(`Launch failed: ${err.message || err}`, 'error');
        btnLaunch.disabled = false;
      }
    };
  }
};
```

### src/services/hf-cdn.js
```js
export const HFCDN = {
  // HF CDN-bypass file-list: single API call -> CDN-only manifest.
  // Returns { repo, path, files: [{ path, size, type, url }], manifest }
  async listAndSaveManifest({ repo, path = null }) {
    // Validate repo format minimally to avoid obvious mistakes
    if (!repo || !/^[\w.-]+\/[\w.-]+(?:\/[\w.-]+)*$/.test(repo)) {
      throw new Error('Invalid repo format. Expected datasets/username/repo or org/repo');
    }

    // Use Hugging Face dataset/tree API (CDN) to list files.
    // This avoids repeated per-file requests and reduces 429 risk.
    const apiBase = 'https://datasets-server.huggingface.co';
    const listEndpoint = `${apiBase}/rows`;
    const params = new URLSearchParams({
      dataset: repo,
      config: '_',
      split: 'train'
    });

    // If path provided, we filter client-side after listing (common lightweight approach).
    // For large repos, prefer repo-specific tree endpoint if available; fallback to rows.
    const res = await fetch
