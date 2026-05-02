# airship / frontend

## Final consolidated plan (highest-value, ≤2h)

**Goal:** Ship a frontend-safe `airship discover` orchestrator that produces a static, cacheable status page and a CDN-only manifest so the frontend can render health/state without runtime backend queries.

---

### Core behavior (non-negotiable)

- Runs **market research** (if present) and **knowledge-RAG top-hub query** → tagged insights (JSON).
- Calls **HF `list_repo_tree` once per date folder** (non-recursive) and writes a CDN-only manifest.
- Emits a **static status page** (`status.html`) and machine-readable `status.json` + `manifest.json`.
- Training and frontend consume **CDN URLs only** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero API calls during load.
- Cron-safe: `#!/usr/bin/env node`, executable, `SHELL=/bin/bash` in crrontab.

---

### File layout

```
/opt/axentx/airship/
├── frontend/
│   ├── src/
│   │   └── orchestrators/
│   │       └── discover.js          # new orchestrator (Node)
│   └── public/
│       ├── status.html              # generated static page
│       ├── status.json              # generated machine status
│       └── manifests/
│           └── {date}/
│               └── files.json        # CDN-only manifest
```

---

### Implementation steps (timeboxed)

| Step | Owner | Time | Details |
|------|-------|------|---------|
| 1 | FE | 15m | Add static output paths (`public/status.html`, `public/status.json`, `public/manifests/{date}/files.json`). Frontend dev server/nginx serves these as static files. |
| 2 | FE | 20m | Create `src/orchestrators/discover.js` (Node) that runs: market script → knowledge-RAG → HF list → writes manifest + insights. |
| 3 | FE | 15m | Ensure training script and frontend consume CDN URLs only (no HF API calls at runtime). |
| 4 | FE | 20m | Make script cron-safe: shebang `#!/usr/bin/env node`, `chmod +x`, and document crontab with `SHELL=/bin/bash`. |
| 5 | FE | 20m | Wire frontend status panel to `/status.html` (or fetch `/status.json`) and poll every ~30s if desired. |
| 6 | FE | 10m | Add lightweight health badges and error handling (exit codes: 0=success, 1=partial, 2=hard failure). |

Total: ~1h40m (≤2h).

---

### CLI and invocation

```bash
# Usage
node src/orchestrators/discover.js [--date YYYY-MM-DD] [--out ./public/status.html]

# Example cron (runs daily at 02:15)
SHELL=/bin/bash
15 2 * * * cd /opt/axentx/airship && node frontend/src/orchestrators/discover.js --date $(date +\%F) --out ./public/status.html >> /var/log/airship-discover.log 2>&1
```

Environment (optional):
- `HF_REPO` — e.g. `datasets/axentx/surrogate` (default can be configured).
- `HF_TOKEN` — only if accessing private repos (public repos use CDN without token).

---

### Code: `frontend/src/orchestrators/discover.js`

```js
#!/usr/bin/env node
/**
 * airship discover
 * - Runs market research (if available) + knowledge-RAG top-hub query
 * - Produces HF CDN-only manifest for a date folder
 * - Emits static status.html + status.json + manifest
 *
 * Usage:
 *   node discover.js [--date YYYY-MM-DD] [--out ./public/status.html]
 *
 * Cron-safe: invoke via bash and ensure SHELL=/bin/bash in crontab.
 */

import fs from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';
import { spawn } from 'child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const projectRoot = path.resolve(__dirname, '../../..');

const DEFAULT_DATE = new Date().toISOString().slice(0, 10);
const DEFAULT_OUT = path.resolve(projectRoot, 'frontend/public/status.html');
const DEFAULT_MANIFEST_DIR = path.resolve(projectRoot, 'frontend/public/manifests');

// Config via env
const HF_REPO = process.env.HF_REPO || 'datasets/example/repo'; // e.g. "datasets/axentx/surrogate"
const HF_API_BASE = 'https://huggingface.co/api';
const HF_CDN_BASE = 'https://huggingface.co/datasets';

function parseArgs() {
  const args = process.argv.slice(2);
  const opts = { date: DEFAULT_DATE, out: DEFAULT_OUT };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--date' && args[i + 1]) { opts.date = args[++i]; }
    else if (args[i] === '--out' && args[i + 1]) { opts.out = path.resolve(args[++i]); }
  }
  opts.manifestDir = path.join(path.dirname(opts.out), 'manifests', opts.date);
  opts.manifestPath = path.join(opts.manifestDir, 'files.json');
  opts.statusJsonPath = path.join(path.dirname(opts.out), 'status.json');
  return opts;
}

async function exists(p) {
  try { await fs.access(p); return true; } catch { return false; }
}

function spawnScript(scriptPath, label) {
  return new Promise((resolve) => {
    if (!fs.access) {
      // defensive: ensure fs.promises exists in this scope (it does)
      resolve({ ok: false, output: null, error: 'fs unavailable', code: 1 });
      return;
    }
    fs.access(scriptPath).then(() => {
      const proc = spawn(scriptPath, { stdio: ['ignore', 'pipe', 'pipe'], shell: '/bin/bash' });
      const chunks = [];
      const errs = [];
      proc.stdout.on('data', (c) => chunks.push(c));
      proc.stderr.on('data', (c) => errs.push(c));
      proc.on('close', (code) => {
        const out = Buffer.concat(chunks).toString().trim();
        const err = Buffer.concat(errs).toString().trim();
        resolve({ ok: code === 0, output: out, error: err || null, code });
      });
      proc.on('error', (err) => resolve({ ok: false, output: null, error: err.message, code: 1 }));
    }).catch(() => {
      resolve({ ok: false, output: null, error: 'not_found', code: 1 });
    });
  });
}

async function listRepoTree(dateFolder) {
  // Single non-recursive folder listing to minimize rate-limit risk.
  const apiUrl = `${HF_API_BASE}/${HF_REPO}/tree/${dateFolder}?recursive=false`;
  const res = await fetch(apiUrl, { headers: { Accept: 'application/json' } });

  if (!res.ok) {
    const txt = await res.text().catch(() => '');
    throw new Error(`HF list_repo_tree failed: ${res.status} ${res.statusText} ${txt}`);
  }

  const items = await res.json();
  if (!Array.isArray(items)) throw new Error('Unexpected HF tree response format');

  const manifest = {};
  for (const item of items) {
    if (item.type !== 'file') continue;
    const cdnUrl = `${HF_CDN_BASE}/${HF_REPO}/resolve/main/${dateFolder}/${item.path}`;
    manifest[item.path] = cdnUrl;
  }
  return manifest;
}

function extractTaggedLines(text, tagPrefixes) {
  const lines = text.split(/\r?\n/);
  const found = [];
  for (const line of lines) {
    for (const prefix of tagPrefixes) {
      if (line.includes(prefix)) {
        found.push(line.trim());
        break;
      }
    }
  }
  return found;
}

function renderHtml(status) {
  const esc = (x
