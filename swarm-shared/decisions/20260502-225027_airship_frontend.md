# airship / frontend

## Final Implementation Plan — Unified, Correct, Actionable (≤2h)

**Single source of truth:**  
A deterministic, CDN-cacheable status snapshot + asset manifest produced by **one Node orchestrator** (`scripts/discover.js`) and consumed by a **tiny browser loader**.  
No SSR, no backend calls during render.

---

### 1) Orchestrator — `scripts/discover.js`

**Why Node over Bash:**  
- Portable JSON handling (no `jq`/sed fragile fallbacks).  
- Same runtime as most frontend tooling (no extra infra).  
- Easier to extend (versions, retries, structured logging).

```js
// scripts/discover.js
// Usage: node scripts/discover.js
// Outputs: dist/status.json, dist/manifest.json, dist/status.html
// Designed for CI: no interactive deps, deterministic, fast.

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import crypto from 'crypto';
import http from 'http';
import https from 'https';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const DIST = path.resolve(ROOT, 'dist');
const PUBLIC = path.resolve(ROOT, 'public');

const OUT_DIR = DIST;
const STATUS_FILE = path.join(OUT_DIR, 'status.json');
const MANIFEST_FILE = path.join(OUT_DIR, 'manifest.json');
const STATUS_PAGE = path.join(OUT_DIR, 'status.html');

const TS = new Date().toISOString();

const SERVICES = {
  arkship: process.env.ARKSHIP_URL || 'http://localhost:8000/health',
  surrogate: process.env.SURROGATE_URL || 'http://localhost:8001/health',
  ui: process.env.UI_URL || 'http://localhost:3000'
};

function httpGet(url, timeoutMs = 3000) {
  return new Promise((resolve) => {
    const lib = url.startsWith('https') ? https : http;
    const req = lib.get(url, { timeout: timeoutMs }, (res) => {
      res.on('data', () => {});
      res.on('end', () => resolve({ code: res.statusCode, ok: res.statusCode >= 200 && res.statusCode < 400 }));
    });
    req.on('error', () => resolve({ code: 503, ok: false }));
    req.on('timeout', () => { req.destroy(); resolve({ code: 504, ok: false }); });
  });
}

function hashFile(filePath) {
  return new Promise((resolve) => {
    const hash = crypto.createHash('sha256');
    const stream = fs.createReadStream(filePath);
    stream.on('data', (d) => hash.update(d));
    stream.on('end', () => resolve(hash.digest('hex')));
    stream.on('error', () => resolve(null));
  });
}

async function buildStatus() {
  const services = {};
  for (const [name, url] of Object.entries(SERVICES)) {
    const { code, ok } = await httpGet(url);
    services[name] = {
      url,
      state: ok ? 'healthy' : 'unreachable',
      code
    };
  }
  return {
    generated_at: TS,
    services
  };
}

async function buildManifest() {
  const assets = {};
  const assetRoots = [PUBLIC].filter((dir) => fs.existsSync(dir));

  for (const assetRoot of assetRoots) {
    const walk = (dir) => {
      const items = fs.readdirSync(dir, { withFileTypes: true });
      for (const item of items) {
        const full = path.join(dir, item.name);
        if (item.isDirectory()) {
          walk(full);
        } else if (item.isFile()) {
          const rel = path.relative(ROOT, full).replace(/\\/g, '/');
          // Skip discover outputs to avoid recursion
          if (rel.startsWith('dist/discover') || rel.startsWith('dist/status')) continue;
          // Only include likely frontend assets
          if (/\.(js|css|html|json|svg|png|ico|webmanifest|map)$/i.test(rel) || rel.endsWith('/robots.txt')) {
            const size = fs.statSync(full).size;
            // We'll hash only a sample set for speed in CI; for full determinism, keep hash.
            // If speed is critical, omit hash or compute in CI artifact step.
            assets[rel] = { size };
          }
        }
      }
    };
    walk(assetRoot);
  }

  // Optional: add top-level routes hint for frontend routing (static)
  const routes = ['/', '/status', '/surrogate', '/arkship'];

  return {
    generated_at: TS,
    assets,
    routes
  };
}

async function main() {
  try {
    if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });

    const [status, manifest] = await Promise.all([buildStatus(), buildManifest()]);

    fs.writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2), 'utf8');
    fs.writeFileSync(MANIFEST_FILE, JSON.stringify(manifest, null, 2), 'utf8');

    // Minimal static status page (CDN-cacheable, zero backend)
    const html = `<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Arkship — Status</title>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <style>
    body{font-family:system-ui,sans-serif;margin:2rem}
    .svc-badge{display:inline-block;margin:0.25rem 0.5rem 0.25rem 0;padding:0.25rem 0.6rem;border-radius:4px;font-size:0.85rem}
    .healthy{background:#d4edda;color:#155724}
    .unhealthy{background:#f8d7da;color:#721c24}
  </style>
</head>
<body>
  <h1>Arkship — Service Status</h1>
  <div id="status">Loading...</div>
  <script>
    fetch('./status.json').then(r=>r.json()).then(d=>{
      const box=document.getElementById('status');
      box.innerHTML=Object.entries(d.services||{}).map(([n,s])=>
        '<span class="svc-badge '+(s.state==='healthy'?'healthy':'unhealthy')+'">'+n+': '+s.state+' ('+s.code+') — <a href="'+s.url+'">'+s.url+'</a></span>'
      ).join('');
    }).catch(()=>{document.getElementById('status').innerText='Unable to load status.'});
  </script>
</body>
</html>`;
    fs.writeFileSync(STATUS_PAGE, html, 'utf8');

    console.log('[airship-discover] Wrote', STATUS_FILE);
    console.log('[airship-discover] Wrote', MANIFEST_FILE);
    console.log('[airship-discover] Wrote', STATUS_PAGE);
    console.log('[airship-discover] Done.');
  } catch (err) {
    console.error('[airship-discover] Error:', err);
    process.exit(1);
  }
}

main();
```

**Make executable (optional wrapper):**  
If you prefer a CLI, add a small Bash wrapper at `bin/airship-discover`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
node scripts/discover.js
```

Then:

```bash
chmod +x bin/airship-discover
```

---

### 2) Frontend integration — lightweight loader

Place this in your frontend entry or as a module loaded by `index.html`:

```js
// frontend/discover-loader.js
// Loads CDN-safe status + manifest. Zero backend coupling.

export async function loadDiscover(basePath = '/dist') {
  try {
    const [statusRes, manifestRes] = await Promise.allSettled([
      fetch(`${basePath}/status.json`, { cache: 'no-cache' }),
      fetch(`${basePath}/manifest.json`, { cache: 'no-cache' })
    ]);

    const status = statusRes.status === 'ful
