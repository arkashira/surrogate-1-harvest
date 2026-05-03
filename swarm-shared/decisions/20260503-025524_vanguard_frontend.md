# vanguard / frontend

## 1. Diagnosis

- No deterministic asset manifest at build time → frontend cannot resolve CDN URLs without runtime HF API calls (invites 429s and breaks CDN-first strategy).
- Missing mount point / entrypoint boundary → no clear separation between orchestration host and browser runtime; frontend likely relies on ad-hoc globals or inline scripts.
- No content-hash in asset filenames → cache-busting is unreliable and CDN caching is suboptimal.
- No Subresource Integrity (SRI) for external/CDN assets → security and cache poisoning risk.
- No lightweight build-time file listing step → training/inference scripts cannot embed a static file list to avoid HF API during data load (violates CDN-bypass pattern).

## 2. Proposed change

Add a deterministic build-time manifest for frontend assets and a small runtime resolver that uses only CDN URLs (no HF API). Scope:

- `/opt/axentx/vanguard/frontend/` (create if absent)
  - `build-manifest.js` — Node script that walks `src/` and `static/`, produces `dist/manifest.json` with `{ "main.js": "/cdn/vanguard/main.[hash].js", "main.css": "/cdn/vanguard/main.[hash].css", ... }`
  - `index.html` — reference assets via manifest keys (or inline a tiny resolver)
  - `src/` and `static/` — source files (existing or scaffolded)
  - `dist/` — build output (gitignored)
- Add npm scripts: `build:manifest`, `build:assets`, `dev`.

## 3. Implementation

```bash
cd /opt/axentx/vanguard
mkdir -p frontend/src frontend/static frontend/dist
```

### package.json (add if absent or update scripts)

```json
{
  "name": "vanguard-frontend",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "build:manifest": "node frontend/build-manifest.js",
    "build:assets": "npm run build:manifest && echo \"Add bundler (esbuild/vite) here\"",
    "dev": "npm run build:manifest && echo \"Start dev server here\""
  }
}
```

### frontend/build-manifest.js

```js
#!/usr/bin/env node
/**
 * Build-time deterministic asset manifest for CDN-first frontend.
 * Outputs dist/manifest.json mapping logical names to CDN paths with content hash.
 *
 * Usage:
 *   node build-manifest.js
 *
 * Notes:
 * - Uses content hash for cache-busting.
 * - Produces paths suitable for CDN hosting at /cdn/vanguard/<file>.
 * - No HF API calls during build or runtime.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const SRC_DIR = path.resolve(__dirname, 'src');
const STATIC_DIR = path.resolve(__dirname, 'static');
const DIST_DIR = path.resolve(__dirname, 'dist');
const CDN_PREFIX = '/cdn/vanguard';

function hashFile(filePath) {
  const buf = fs.readFileSync(filePath);
  return crypto.createHash('sha256').update(buf).digest('hex').slice(0, 12);
}

function ensureDist() {
  if (!fs.existsSync(DIST_DIR)) fs.mkdirSync(DIST_DIR, { recursive: true });
}

function collectFiles(dir) {
  if (!fs.existsSync(dir)) return [];
  const results = [];
  function walk(d) {
    for (const entry of fs.readdirSync(d)) {
      const full = path.join(d, entry);
      const stat = fs.statSync(full);
      if (stat.isDirectory()) walk(full);
      else results.push(full);
    }
  }
  walk(dir);
  return results;
}

function buildManifest() {
  const manifest = {};
  const allFiles = [...collectFiles(SRC_DIR), ...collectFiles(STATIC_DIR)];

  for (const file of allFiles) {
    const rel = path.relative(process.cwd(), file);
    const ext = path.extname(file);
    const base = path.basename(file, ext);
    const hash = hashFile(file);
    const cdnName = `${base}.${hash}${ext}`;
    const cdnPath = `${CDN_PREFIX}/${cdnName}`;

    // Logical key: e.g. "main.js", "static/logo.png"
    const logical = path.relative(SRC_DIR, file).startsWith('..')
      ? path.join('static', path.relative(STATIC_DIR, file))
      : path.relative(SRC_DIR, file);

    manifest[logical] = cdnPath;

    // Copy to dist with hashed name (simple asset emit)
    const outPath = path.join(DIST_DIR, cdnName);
    fs.copyFileSync(file, outPath);
  }

  // Add index mapping for convenience
  manifest._index = {};
  for (const [k, v] of Object.entries(manifest)) {
    if (k.startsWith('_')) continue;
    const name = path.basename(k);
    if (!manifest._index[name]) manifest._index[name] = [];
    manifest._index[name].push(v);
  }

  const manifestPath = path.join(DIST_DIR, 'manifest.json');
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
  console.log(`Manifest written to ${manifestPath}`);
  return manifest;
}

if (require.main === module) {
  ensureDist();
  buildManifest();
}

module.exports = { buildManifest };
```

### frontend/index.html (minimal example)

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard</title>
  <!-- Runtime resolver: picks first matching asset from manifest for this key -->
  <script>
    // In production, inline a minimal, versioned manifest or fetch /cdn/vanguard/manifest.json once and cache.
    // This example assumes manifest.json is available at build time and injected or deployed alongside.
    window.VANGUARD_CONFIG = window.VANGUARD_CONFIG || {};
  </script>
  <!-- Example: hashed CSS (if produced) -->
  <!-- <link rel="stylesheet" href="/cdn/vanguard/main.<hash>.css" integrity="sha384-..."> -->
</head>
<body>
  <div id="app">Loading...</div>
  <!-- Example: hashed JS -->
  <!-- <script src="/cdn/vanguard/main.<hash>.js" integrity="sha384-..." defer></script> -->
  <script>
    // Lightweight runtime resolver (CDN-only, no HF API)
    (function () {
      var manifest = window.VANGUARD_MANIFEST || {};
      function resolve(key) {
        if (manifest[key]) return manifest[key];
        // fallback: try index lookup
        var name = key.split('/').pop();
        if (manifest._index && manifest._index[name]) return manifest._index[name][0];
        return null;
      }
      window.VANGUARD_RESOLVE = resolve;
      // Example usage:
      // var mainJs = resolve('main.js');
      // if (mainJs) { var s=document.createElement('script');s.src=mainJs;s.defer=true;document.head.appendChild(s); }
    })();
  </script>
</body>
</html>
```

### .gitignore additions

```
frontend/dist/
node_modules/
```

## 4. Verification

1. Run build:
   ```bash
   cd /opt/axentx/vanguard
   npm run build:manifest
   ```
   Confirm `frontend/dist/manifest.json` exists and contains CDN paths with hashes.

2. Check CDN paths:
   ```bash
   cat frontend/dist/manifest.json | head -20
   ```
   Ensure paths follow `/cdn/vanguard/<name>.<hash>.<ext>` and no HF API URLs appear.

3. Validate deterministic output:
   ```bash
   cp frontend/dist/manifest.json /tmp/m1.json
   npm run build:manifest
   diff /tmp/m1.json frontend/dist/manifest.json
   ```
   No diffs expected for identical source files.

4. Runtime test (local):
   - Serve `frontend/dist` statically (e.g., `npx serve frontend/dist` or copy into a CDN-served location).
   - Open `index.html` and check console for `VANGUARD_RESOLVE` availability.
   - Confirm
