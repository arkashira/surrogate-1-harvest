# vanguard / frontend

## Final synthesized solution (correct + actionable)

**Core diagnosis (agreed across candidates)**  
- Frontend/server still calls authenticated HF API (`list_repo_tree`, `/api/...`) on every page/training load → burns 1000/5min quota → 429s.  
- No persisted file manifest → every visit re-enumerates via API.  
- File fetches use authenticated `/api/...` paths instead of public CDN URLs → triggers auth rate limits.  
- No graceful fallback when HF API is unavailable.  
- Missing shebang/permissions hygiene in cron/exec wrappers.

**Chosen approach**  
- Use a **static, build/CI-time manifest** (not runtime server API calls) to eliminate quota burn in production.  
- Frontend loads **only the manifest + public CDN URLs** (no Authorization header).  
- Add **client-side sessionStorage cache** for the manifest to avoid repeated loads in the same workflow.  
- Add **retry/backoff for CDN fetches** and graceful degradation on 429/5xx.  
- Fix cron/exec wrapper shebangs and permissions.

---

### 1) Create HF CDN client (ESM)  
File: `/opt/axentx/vanguard/src/lib/hf-client.js`

```js
/**
 * HF CDN client for vanguard frontend.
 * Uses public CDN URLs and a static manifest to avoid HF API rate limits.
 * Manifest is generated at build/CI time via scripts/generate-hf-manifest.js
 */

let MANIFEST = null;
let MANIFEST_PATH = '/manifests'; // base for CDN-hosted manifests (see step 2)

async function loadManifest(repo, dateFolder) {
  // Try sessionStorage first (per-tab workflow cache)
  const key = `hf-manifest:${repo}:${dateFolder}`;
  const cached = sessionStorage.getItem(key);
  if (cached) {
    MANIFEST = JSON.parse(cached);
    return MANIFEST;
  }

  // Fetch static manifest from CDN (no Authorization)
  const manifestUrl = `/manifests/${repo.replace(/\//g, '_')}/${dateFolder}.json`;
  const res = await fetchWithRetry(manifestUrl, { credentials: 'omit' });
  if (!res.ok) throw new Error(`Failed to load manifest: ${res.status}`);
  MANIFEST = await res.json();
  try { sessionStorage.setItem(key, JSON.stringify(MANIFEST)); } catch (_) {}
  return MANIFEST;
}

function cdnUrl(repo, dateFolder, filePath) {
  // filePath is relative to dateFolder in the repo
  const base = `https://huggingface.co/datasets/${repo}/resolve/main/${dateFolder}`;
  return `${base}/${filePath.replace(/^\/+/, '')}`;
}

async function fetchWithRetry(url, options = {}, retries = 3, backoff = 800) {
  for (let i = 0; i <= retries; i++) {
    try {
      const res = await fetch(url, { ...options, redirect: 'follow' });
      if (res.ok) return res;
      // retry on 429/5xx; do not retry 404
      if (res.status === 404 || (res.status < 500 && res.status !== 429)) return res;
    } catch (err) {
      if (i === retries) throw err;
    }
    if (i < retries) await new Promise((r) => setTimeout(r, backoff * 2 ** i));
  }
  throw new Error(`Failed after ${retries} retries: ${url}`);
}

export async function loadFile(repo, dateFolder, filePath) {
  if (!MANIFEST) await loadManifest(repo, dateFolder);
  const url = cdnUrl(repo, dateFolder, filePath);
  const res = await fetchWithRetry(url, { credentials: 'omit' });
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status} ${url}`);
  return res;
}

export async function loadText(repo, dateFolder, filePath) {
  const res = await loadFile(repo, dateFolder, filePath);
  return res.text();
}

export async function loadJSON(repo, dateFolder, filePath) {
  const res = await loadFile(repo, dateFolder, filePath);
  return res.json();
}

export async function getFilePaths(repo, dateFolder) {
  if (!MANIFEST) await loadManifest(repo, dateFolder);
  return MANIFEST?.files || [];
}
```

---

### 2) Manifest generator (run on Mac/CI)  
File: `/opt/axentx/vanguard/scripts/generate-hf-manifest.js`

```bash
#!/usr/bin/env node
/**
 * Generate a static manifest for repo/dateFolder.
 * Run on Mac/CI when HF API is available.
 *
 * Usage:
 *   node scripts/generate-hf-manifest.js <repo> <dateFolder> [outDir]
 *
 * Example:
 *   node scripts/generate-hf-manifest.js axentx/surrogate-1 2026-04-29 static/manifests
 */

const { HfApi } = require('@huggingface/hub');
const fs = require('fs');
const path = require('path');

async function main() {
  const [repo, dateFolder, outDir = 'static/manifests'] = process.argv.slice(2);
  if (!repo || !dateFolder) {
    console.error('Usage: node generate-hf-manifest.js <repo> <dateFolder> [outDir]');
    process.exit(1);
  }

  const api = new HfApi();
  // List top-level of dateFolder (non-recursive to avoid huge pagination)
  const tree = await api.listRepoTree({ repo, path: dateFolder, recursive: false });
  const files = (tree.files || [])
    .map((f) => path.posix.relative(dateFolder, f.path))
    .filter(Boolean);

  const manifest = {
    repo,
    dateFolder,
    generatedAt: new Date().toISOString(),
    files
  };

  const safeRepo = repo.replace(/\//g, '_');
  const outPath = path.join(outDir, safeRepo);
  fs.mkdirSync(outPath, { recursive: true });
  const outFile = path.join(outPath, `${dateFolder}.json`);
  fs.writeFileSync(outFile, JSON.stringify(manifest, null, 2));
  console.log(`Manifest written to ${outFile} (${files.length} files)`);
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
```

Make executable and install dependency:

```bash
chmod +x /opt/axentx/vanguard/scripts/generate-hf-manifest.js
cd /opt/axentx/vanguard && npm install @huggingface/hub
```

---

### 3) Generate and serve manifests (one-time/CI step)

```bash
cd /opt/axentx/vanguard
node scripts/generate-hf-manifest.js axentx/surrogate-1 2026-04-29 static/manifests
```

- Ensure your dev/prod server serves `static/` at `/` (or adjust `/manifests` path in client accordingly).  
- In production, commit or deploy generated manifests alongside the app to avoid runtime HF API calls entirely.

---

### 4) Cron/exec wrapper hygiene

```bash
# Fix shebangs and permissions for all .sh scripts
for f in /opt/axentx/vanguard/scripts/*.sh; do
  [ -f "$f" ] || continue
  if ! head -1 "$f" | grep -qE '^#!/usr/bin/env bash|^#!/bin/bash'; then
    sed -i '1i #!/usr/bin/env bash' "$f"
  fi
  chmod +x "$f"
done
```

---

### 5) Verification checklist

1. **No HF API calls in production**  
   - Network tab: confirm no requests to `huggingface.co/api/...` or `list_repo_tree`.  
   - File requests go to `https://huggingface.co/datasets/.../resolve/main/...` with no Authorization header.

2. **Manifest present and used**  
   - `static/manifests/axentx_surrogate-1/2026-0
