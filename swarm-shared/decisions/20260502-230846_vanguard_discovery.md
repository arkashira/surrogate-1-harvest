# vanguard / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

I merged both proposals, kept only the **correct, high-leverage** technical choices, and removed contradictions and fluff.

### 1) Diagnosis (resolved)
- ✅ **CDN-bypass ingestion missing** → training hits HF API and burns quota/429s.  
  **Fix**: list once via HF API, then download exclusively via CDN (`resolve/main/...`) with no Authorization header.
- ✅ **No durable run state** → retries are manual and not idempotent.  
  **Fix**: single `.ingest-state.json` per run folder with per-file status (downloaded/skipped/error) so resume is safe.
- ✅ **No pre-listed file manifest** → each training run re-enumerates via API.  
  **Fix**: emit `files.json` (date-folder file list) and a lightweight manifest mapping local names to CDN URLs. Training consumes the manifest, never the API.
- ❌ Drop UI-only notes (hash-router scroll, URL-encoded dataset list) — they’re out of scope for ingestion/training reliability.

### 2) Proposed change (minimal, high-leverage)
Add a small CLI-driven ingestion pipeline:
1. `scripts/list_hf_files.py` — one HF API tree call → cached `batches/mirror-merged/{date}/files.json`.
2. `src/ingest.js` — downloads via CDN, projects to `{prompt,response}`, writes line-delimited JSON (or Parquet), records durable state.
3. `scripts/build_cdn_train_manifest.py` — converts `files.json` into `manifest.json` (local_path, cdn_url, rows) for Lightning training.
4. `ops/runs/{date}.json` — run-level checkpoint (run_id, date, repo, status, counts) for ops visibility.

CLI:
```bash
node src/ingest.js --date=YYYY-MM-DD --repo=owner/dataset [--out-dir=...]
```
Resume:
```bash
# re-run same command after partial failure → skips completed files
node src/ingest.js --date=YYYY-MM-DD --repo=owner/dataset
```

### 3) Implementation (concrete, correct)

Directory layout:
```
/opt/axentx/vanguard/
├── package.json
├── scripts/
│   ├── list_hf_files.py
│   └── build_cdn_train_manifest.py
├── src/
│   ├── ingest.js
│   └── state.js
└── batches/mirror-merged/
    └── {date}/
        ├── files.json
        ├── manifest.json
        ├── .ingest-state.json
        └── {slug}.jsonl   (or .parquet)
```

`package.json` (additions):
```json
{
  "name": "vanguard",
  "version": "1.0.0",
  "type": "module",
  "bin": {
    "vanguard-ingest": "src/ingest.js"
  },
  "dependencies": {
    "axios": "^1.6.0",
    "commander": "^11.0.0"
  },
  "scripts": {
    "ingest": "node src/ingest.js"
  }
}
```

`src/state.js` (durable per-folder state):
```js
import fs from 'fs';
import path from 'path';

export function loadState(root) {
  const p = path.join(root, '.ingest-state.json');
  if (!fs.existsSync(p)) return { downloaded: {}, startedAt: Date.now() };
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

export function saveState(root, state) {
  const p = path.join(root, '.ingest-state.json');
  fs.writeFileSync(p, JSON.stringify(state, null, 2), 'utf8');
}
```

`src/ingest.js` (CDN-only download + projection + resume):
```js
import { program } from 'commander';
import axios from 'axios';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { loadState, saveState } from './state.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

program
  .requiredOption('--date <YYYY-MM-DD>', 'date folder to ingest')
  .requiredOption('--repo <owner/dataset>', 'HF dataset repo')
  .option('--out-dir <dir>', 'output root', path.join(ROOT, 'batches', 'mirror-merged'))
  .parse();

const opts = program.opts();
const outDir = path.join(opts.outDir, opts.date);
const filesPath = path.join(outDir, 'files.json');
const manifestPath = path.join(outDir, 'manifest.json');
const runStatePath = path.join('ops', 'runs', `${opts.date}.json`);
const state = loadState(outDir);

async function listRepoTree(repo, folder = '') {
  const url = `https://huggingface.co/api/datasets/${repo}/tree/${encodeURIComponent(folder)}`;
  const res = await axios.get(url);
  return res.data;
}

function projectToPair(raw, filePath) {
  try {
    const lines = raw.toString().trim().split('\n').filter(Boolean);
    const pairs = lines.map(l => {
      const obj = JSON.parse(l);
      return { prompt: obj.prompt || obj.input || '', response: obj.response || obj.output || '' };
    }).filter(p => p.prompt || p.response);
    if (pairs.length) return pairs;
  } catch {
    // fallback: single pair
  }
  return [{ prompt: raw.toString().slice(0, 2000), response: '' }];
}

function slugify(file) {
  return file.replace(/[^a-z0-9]/gi, '_').toLowerCase().replace(/^_+|_+$/g, '');
}

async function ensureOpsRunState(date, repo) {
  const p = path.resolve(ROOT, runStatePath);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  if (fs.existsSync(p)) return JSON.parse(fs.readFileSync(p, 'utf8'));
  const init = { run_id: `${date}-${Date.now()}`, date, repo, status: 'running', counts: { total: 0, done: 0, errors: 0 } };
  fs.writeFileSync(p, JSON.stringify(init, null, 2), 'utf8');
  return init;
}

async function updateOpsRunState(date, update) {
  const p = path.resolve(ROOT, runStatePath);
  if (!fs.existsSync(p)) return;
  const st = JSON.parse(fs.readFileSync(p, 'utf8'));
  Object.assign(st, update);
  if (update.counts) Object.assign(st.counts, update.counts);
  fs.writeFileSync(p, JSON.stringify(st, null, 2), 'utf8');
}

async function run() {
  console.log(`Ingesting ${opts.repo} @ ${opts.date}`);
  fs.mkdirSync(outDir, { recursive: true });
  const opsState = await ensureOpsRunState(opts.date, opts.repo);

  // 1) list once, cache
  let files;
  if (fs.existsSync(filesPath)) {
    files = JSON.parse(fs.readFileSync(filesPath, 'utf8'));
    console.log(`Loaded cached file list (${files.length} files)`);
  } else {
    const tree = await listRepoTree(opts.repo, opts.date);
    files = tree.map(n => n.path);
    fs.writeFileSync(filesPath, JSON.stringify(files, null, 2));
    console.log(`Saved file list (${files.length} files)`);
  }
  await updateOpsRunState(opts.date, { counts: { total: files.length } });

  // 2) download & project
  const manifest = [];
  for (const file of files) {
    if (state.downloaded[file] && state.downloaded[file].status === 'done') {
      // still include in manifest
      const slug = slugify(file);
      manifest.push({
        local_path: path.join(outDir, `${slug}.jsonl`),
        cdn_url: `https://huggingface.co/datasets/${opts.repo}/resolve/main/${encodeURIComponent(file)}`,
        rows: state.downloaded[file].pairs || 0
      });
      continue;
    }
    try {
      const cdnUrl = `https
