# airship / frontend

## Final Implementation Plan — Frontend-safe `airship discover` orchestrator

**Goal (<2h):** Deterministic, CDN-cacheable status snapshot + asset manifest so the frontend can render health/state without SSR/backend calls.

---

### 1) CLI orchestrator — `scripts/airship-discover.js`

- Use **Node** (not Bash) for portability, JSON safety, and easier extensibility.
- Shebang `#!/usr/bin/env node`, executable.
- Outputs to `public/` (not `dist/`) so static hosting/CDN picks it up naturally.
- Deterministic keys, no secrets, only localhost checks.
- Exits non-zero on failure so CI blocks deploys.

```js
#!/usr/bin/env node
/**
 * airship discover
 * Generates:
 *  - public/status-snapshot.json
 *  - public/asset-manifest.json
 *
 * Usage: node scripts/airship-discover.js
 * CI: run before build/deploy.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import http from 'http';
import https from 'https';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const ROOT = path.resolve(__dirname, '..');
const PUBLIC_DIR = path.join(ROOT, 'public');
const STATUS_FILE = path.join(PUBLIC_DIR, 'status-snapshot.json');
const MANIFEST_FILE = path.join(PUBLIC_DIR, 'asset-manifest.json');

function nowISO() {
  return new Date().toISOString();
}

function httpCheck(url, timeout = 2000) {
  return new Promise((resolve) => {
    const lib = url.startsWith('https') ? https : http;
    const req = lib.get(url, { timeout }, (res) => {
      req.destroy();
      resolve(res.statusCode >= 200 && res.statusCode < 400 ? 'healthy' : 'unhealthy');
    });
    req.on('error', () => resolve('unhealthy'));
    req.on('timeout', () => {
      req.destroy();
      resolve('unhealthy');
    });
  });
}

async function discoverServices() {
  const endpoints = {
    'arkship-ui': process.env.ARKSHIP_UI_URL || 'http://localhost:3000',
    'arkship-api': process.env.ARKSHIP_API_URL || 'http://localhost:8000',
    'surrogate-ai': process.env.SURROGATE_AI_URL || 'http://localhost:8001',
  };

  const checks = await Promise.all(
    Object.entries(endpoints).map(async ([name, url]) => ({
      name,
      url,
      health: await httpCheck(url),
      status: 'unknown', // kept for compatibility; can be mapped from health
      version: process.env[`${name.toUpperCase()}_VERSION`] || 'unknown',
    }))
  );

  return checks;
}

function systemInfo() {
  return {
    arch: process.arch,
    os: process.platform,
    kernel: '', // not reliably available cross-platform in Node; omit or use os.release()
    generatedAt: nowISO(),
    environment: process.env.NODE_ENV || 'development',
  };
}

function writeJSON(file, payload) {
  fs.writeFileSync(file, JSON.stringify(payload, null, 2), 'utf8');
}

function buildAssetManifest() {
  // If a build tool already emitted a manifest, prefer it.
  const possibleManifests = [
    path.join(PUBLIC_DIR, 'asset-manifest.json'),
    path.join(ROOT, 'build', 'asset-manifest.json'),
    path.join(ROOT, 'dist', 'asset-manifest.json'),
  ];

  for (const p of possibleManifests) {
    if (fs.existsSync(p)) {
      const existing = JSON.parse(fs.readFileSync(p, 'utf8'));
      // Ensure deterministic top-level keys
      return {
        generatedAt: nowISO(),
        entrypoints: existing.entrypoints || { main: 'index.html' },
        chunks: existing.chunks || [],
        js: existing.js || [],
        css: existing.css || [],
        images: existing.images || [],
      };
    }
  }

  // Fallback: minimal deterministic manifest from public/ static files
  const files = fs.existsSync(PUBLIC_DIR) ? fs.readdirSync(PUBLIC_DIR) : [];
  const js = files.filter((f) => f.endsWith('.js')).map((f) => `/${f}`);
  const css = files.filter((f) => f.endsWith('.css')).map((f) => `/${f}`);
  const images = files.filter((f) => /\.(png|jpe?g|gif|svg|webp)$/i.test(f)).map((f) => `/${f}`);

  return {
    generatedAt: nowISO(),
    entrypoints: { main: 'index.html' },
    chunks: [],
    js,
    css,
    images,
  };
}

async function main() {
  try {
    fs.mkdirSync(PUBLIC_DIR, { recursive: true });

    const services = await discoverServices();
    const system = systemInfo();

    const statusSnapshot = {
      generatedAt: nowISO(),
      services,
      system,
    };

    writeJSON(STATUS_FILE, statusSnapshot);
    writeJSON(MANIFEST_FILE, buildAssetManifest());

    console.log('✅ Generated status snapshot:', path.relative(ROOT, STATUS_FILE));
    console.log('✅ Generated asset manifest:', path.relative(ROOT, MANIFEST_FILE));
  } catch (err) {
    console.error('❌ Failed to generate discovery outputs:', err);
    process.exit(1);
  }
}

if (process.argv[1] === __filename) {
  main();
}
```

---

### 2) Frontend loader — `src/lib/loadStatusSnapshot.js`

- CDN-safe: fetches `/status-snapshot.json` and `/asset-manifest.json`.
- Synchronous cache after first load for rendering.
- Small, robust, with timeouts and sensible defaults.

```js
// src/lib/loadStatusSnapshot.js
// Lightweight CDN-safe loader for status snapshot + asset manifest.
// Designed for browser usage (no SSR/backend required).

const DEFAULT_SNAPSHOT = {
  generatedAt: null,
  services: [
    { name: 'arkship-ui', url: '/', health: 'unknown', status: 'unknown', version: 'unknown' },
    { name: 'arkship-api', url: '/api', health: 'unknown', status: 'unknown', version: 'unknown' },
    { name: 'surrogate-ai', url: '/ai', health: 'unknown', status: 'unknown', version: 'unknown' },
  ],
  system: { environment: 'unknown' },
};

const DEFAULT_MANIFEST = {
  generatedAt: null,
  entrypoints: { main: 'index.html' },
  chunks: [],
  js: [],
  css: [],
  images: [],
};

let snapshotCache = null;
let manifestCache = null;

export async function loadStatusSnapshot(options = {}) {
  const {
    statusPath = '/status-snapshot.json',
    manifestPath = '/asset-manifest.json',
    timeout = 3000,
  } = options;

  if (snapshotCache && manifestCache) {
    return { snapshot: snapshotCache, manifest: manifestCache };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  try {
    const [sRes, mRes] = await Promise.allSettled([
      fetch(statusPath, { signal: controller.signal, cache: 'no-store' }),
      fetch(manifestPath, { signal: controller.signal, cache: 'no-store' }),
    ]);

    snapshotCache =
      sRes.status === 'fulfilled' && sRes.value.ok
        ? await sRes.value.json().catch(() => DEFAULT_SNAPSHOT)
        : DEFAULT_SNAPSHOT;

    manifestCache =
      mRes.status === 'fulfilled' && mRes.value.ok
        ? await mRes.value.json().catch(() => DEFAULT_MANIFEST)
        : DEFAULT_MANIFEST;
  } catch {
    snapshotCache = DEFAULT_SNAPSHOT;
    manifestCache = DEFAULT_MANIFEST;
  } finally {
    clearTimeout(timer);
  }

  return { snapshot: snapshotCache, manifest:
