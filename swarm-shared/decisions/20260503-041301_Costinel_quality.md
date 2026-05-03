# Costinel / quality

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a “Top Hub Insight” quality panel to Costinel that surfaces the most-connected hub (e.g., “MOC”) from the knowledge-rag graph with zero runtime HF API calls, CDN-first data loading, and robust fallback. Ships as a standalone React component + build script.

### Scope (what will ship)
- `scripts/build-top-hub.js` — Mac-side orchestration: list date folder, save `top-hub.json` to `public/data/` (one API call per build).
- `src/components/TopHubSignalPanel.jsx` — React panel: CDN fetch `public/data/top-hub.json`, render card with hub name, score, related docs, and actionable signal; graceful fallback states.
- `public/data/top-hub.json` (generated) — minimal schema `{ hub, score, relatedDocs: [{ title, url, relevance }], generatedAt }`.
- No runtime HF API. CDN path: `https://huggingface.co/datasets/{repo}/resolve/main/data/top-hub.json` (or relative `/data/top-hub.json`).

### Architecture decisions
- CDN-first: training/UI reads from CDN URLs; no `/api/` during runtime.
- Build-time list: single `list_repo_tree` per date folder on Mac, commit `top-hub.json` (or push to HF repo).
- Fallbacks: local file → CDN → static placeholder → empty state.
- Schema minimal: only fields needed for signal panel.

### Steps (≤2h)
1. Create `scripts/build-top-hub.js` (Node/Bash-friendly) that:
   - Uses HF API once to `list_repo_tree(path=data/top-hub, recursive=false)` for current date folder.
   - Picks latest `top-hub.json` and copies to `public/data/top-hub.json` (or writes fresh if absent).
   - If API unavailable, generates minimal placeholder.
2. Add `src/components/TopHubSignalPanel.jsx`:
   - Fetch `/data/top-hub.json` (CDN or local).
   - States: loading → success → error/empty.
   - Render card: hub name, score, list of related docs, timestamp.
   - Action: “View in knowledge-rag” link (if applicable).
3. Wire panel into existing quality dashboard (import + place in grid).
4. Update `package.json` scripts: `"build:top-hub": "node scripts/build-top-hub.js"`.
5. Verify locally, commit.

---

## scripts/build-top-hub.js
```js
#!/usr/bin/env node
/**
 * Build script: fetch latest top-hub.json from HF repo (one API call)
 * and place into public/data/top-hub.json for CDN-first runtime usage.
 *
 * Usage:
 *   HUGGING_FACE_TOKEN=hf_xxx node scripts/build-top-hub.js
 *
 * Notes:
 * - Uses HF CDN URLs for runtime (zero API calls in UI).
 * - If API fails, writes a minimal placeholder so UI still renders.
 */

const fs = require('fs');
const path = require('path');
const https = require('https');

const REPO = process.env.HF_REPO || 'datasets/axentx/costinel'; // or your repo
const TOKEN = process.env.HUGGING_FACE_TOKEN || '';
const OUT_DIR = path.join(__dirname, '..', 'public', 'data');
const OUT_FILE = path.join(OUT_DIR, 'top-hub.json');

function hfRequest(pathname) {
  return new Promise((resolve, reject) => {
    const options = {
      hostname: 'huggingface.co',
      port: 443,
      path: pathname,
      method: 'GET',
      headers: {
        'User-Agent': 'CostinelBuildScript/1.0',
        ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
      },
    };

    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', (chunk) => (data += chunk));
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try {
            resolve(JSON.parse(data));
          } catch (e) {
            reject(new Error(`Invalid JSON from HF: ${e.message}`));
          }
        } else {
          reject(new Error(`HF request failed: ${res.statusCode} ${data}`));
        }
      });
    });

    req.on('error', reject);
    req.setTimeout(15000, () => {
      req.destroy();
      reject(new Error('HF request timeout'));
    });
    req.end();
  });
}

async function listRepoTree(folder = 'data/top-hub') {
  // non-recursive to avoid pagination/rate limits
  const encoded = encodeURIComponent(`${REPO}/tree/main/${folder}`);
  return hfRequest(`/api/datasets/${encoded}?recursive=false`);
}

async function downloadFile(filePath) {
  // CDN URL — does NOT count against API rate limits
  const cdnUrl = `https://huggingface.co/datasets/${REPO}/resolve/main/${filePath}`;
  return new Promise((resolve, reject) => {
    https
      .get(cdnUrl, (res) => {
        if (res.statusCode === 200) {
          let data = '';
          res.setEncoding('utf8');
          res.on('data', (chunk) => (data += chunk));
          res.on('end', () => resolve(data));
        } else {
          reject(new Error(`CDN download failed: ${res.statusCode}`));
        }
      })
      .on('error', reject)
      .setTimeout(15000, () => reject(new Error('CDN timeout')));
  });
}

function ensureOutDir() {
  if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });
}

function writePlaceholder() {
  const placeholder = {
    hub: 'MOC',
    score: 0.82,
    relatedDocs: [
      { title: 'Cloud Governance Playbook', url: '#', relevance: 0.91 },
      { title: 'Cost Anomaly Patterns', url: '#', relevance: 0.87 },
    ],
    generatedAt: new Date().toISOString(),
    note: 'placeholder — no HF data available',
  };
  fs.writeFileSync(OUT_FILE, JSON.stringify(placeholder, null, 2), 'utf8');
  console.log('Wrote placeholder top-hub.json to', OUT_FILE);
}

async function run() {
  ensureOutDir();

  try {
    // List current folder to find latest top-hub.json
    const tree = await listRepoTree('data/top-hub');
    // Expect tree to be array of file objects with path
    const files = Array.isArray(tree) ? tree : tree.files || tree.items || [];
    const jsonFiles = files
      .map((f) => (typeof f === 'string' ? f : f.path))
      .filter((p) => p && p.endsWith('.json'));

    if (jsonFiles.length === 0) {
      console.warn('No JSON files found in data/top-hub; using placeholder.');
      writePlaceholder();
      return;
    }

    // Prefer most recent by name (assumes date in filename) or pick first
    const latest = jsonFiles.sort().reverse()[0];
    console.log(`Downloading ${latest} from CDN...`);
    const content = await downloadFile(latest);
    const parsed = JSON.parse(content);

    // Normalize to minimal schema expected by panel
    const normalized = {
      hub: parsed.hub || parsed.name || 'Unknown',
      score: Number(parsed.score || parsed.relevance || 0),
      relatedDocs: Array.isArray(parsed.relatedDocs || parsed.docs || parsed.related || [])
        ? (parsed.relatedDocs || parsed.docs || parsed.related || []).slice(0, 6).map((d) => ({
            title: d.title || d.name || 'Untitled',
            url: d.url || d.link || '#',
            relevance: Number(d.relevance || d.score || 0.5),
          }))
        : [],
      generatedAt: parsed.generatedAt || new Date().toISOString(),
    };

    fs.writeFileSync(OUT_FILE, JSON.stringify(normalized, null, 2), 'utf8');
    console.log('Wrote top-hub.json to',
