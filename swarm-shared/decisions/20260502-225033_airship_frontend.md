# airship / frontend

## Final Implementation (merged + reconciled)

**Scope (<2h)**  
Ship a frontend-safe `airship discover` orchestrator that produces:

- `dist/discover/status.json` — deterministic service-health snapshot (Arkship + Surrogate) with last-checked timestamp and overall health flag.  
- `dist/discover/manifest.json` — CDN asset manifest (chunks, CSS, precache entries) with SHA-256 integrity hashes and sizes.  
- `dist/discover/status.html` — tiny static status page that renders the snapshot client-side (no SSR/backend) and auto-refreshes.

Frontend (localhost:3000) and Surrogate AI (localhost:8001) remain decoupled; outputs are intended for CDN upload (`Cache-Control` guidance included).

---

### 1) CLI entrypoint (bash)

`bin/airship-discover`

```bash
#!/usr/bin/env bash
# airship-discover — orchestrator for static status+manifest generation
# Usage: ./bin/airship-discover [--out-dir dist/discover] [--env production|development]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

OUT_DIR="${PROJECT_ROOT}/dist/discover"
ENVIRONMENT="development"

# Parse minimal args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --env)     ENVIRONMENT="$2"; shift 2 ;;
    --help|-h) echo "Usage: $0 [--out-dir path] [--env production|development]"; exit 0 ;;
    *)         echo "Unknown option: $1"; exit 1 ;;
  esac
done

mkdir -p "${OUT_DIR}"

# Delegate heavy lifting to Node (more robust JSON/portability)
exec node "${PROJECT_ROOT}/scripts/discover.js" \
  --out-dir "${OUT_DIR}" \
  --env "${ENVIRONMENT}"
```

Make executable:

```bash
chmod +x bin/airship-discover
```

---

### 2) Node orchestrator (portable, deterministic)

`scripts/discover.js`

```js
#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { performance } = require('perf_hooks');

const argv = require('minimist')(process.argv.slice(2), {
  string: ['out-dir', 'env'],
  default: { 'out-dir': 'dist/discover', env: 'development' },
});

const OUT_DIR = path.resolve(argv['out-dir']);
const ENV = argv.env;
const TIMEOUT_MS = 2000;
const ARKSHIP_HEALTH_URL = process.env.ARKSHIP_HEALTH_URL || 'http://localhost:8000/health';
const SURROGATE_HEALTH_URL = process.env.SURROGATE_HEALTH_URL || 'http://localhost:8001/health';

function nowISO() {
  return new Date().toISOString();
}

function safeHash(filePath) {
  try {
    const buf = fs.readFileSync(filePath);
    return crypto.createHash('sha256').update(buf).digest('hex');
  } catch {
    return null;
  }
}

function safeStat(filePath) {
  try {
    return fs.statSync(filePath).size;
  } catch {
    return 0;
  }
}

async function fetchJSON(url) {
  // Use globalThis.fetch if available (Node 18+), otherwise degrade gracefully.
  if (typeof globalThis.fetch === 'function') {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(url, { signal: controller.signal, headers: { Accept: 'application/json' } });
      clearTimeout(id);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch {
      return null;
    }
  }
  // Fallback for older Node: try curl via child_process (best-effort)
  const { execSync } = require('child_process');
  try {
    const out = execSync(`curl -fs --max-time ${Math.floor(TIMEOUT_MS / 1000)} -H "Accept: application/json" "${url}"`, { stdio: 'pipe', encoding: 'utf8' });
    return JSON.parse(out);
  } catch {
    return null;
  }
}

function normalizeService(raw, name) {
  if (!raw || typeof raw !== 'object') {
    return { status: 'unknown', version: 'unknown', uptime: null, timestamp: nowISO(), name };
  }
  return {
    status: (raw.status || raw.state || 'unknown').toString(),
    version: (raw.version || raw.release || 'unknown').toString(),
    uptime: typeof raw.uptime === 'number' ? raw.uptime : (typeof raw.uptime_seconds === 'number' ? raw.uptime_seconds : null),
    timestamp: nowISO(),
    name,
  };
}

async function collectServices() {
  const start = performance.now();
  const [arkshipRaw, surrogateRaw] = await Promise.allSettled([
    fetchJSON(ARKSHIP_HEALTH_URL),
    fetchJSON(SURROGATE_HEALTH_URL),
  ]);
  const elapsed = Math.round(performance.now() - start);

  const arkship = normalizeService(arkshipRaw.status === 'fulfilled' ? arkshipRaw.value : null, 'arkship');
  const surrogate = normalizeService(surrogateRaw.status === 'fulfilled' ? surrogateRaw.value : null, 'surrogate');

  const healthy = ['ok', 'ready'].includes(arkship.status) && ['ok', 'ready'].includes(surrogate.status);

  return {
    schema: 'airship-discover/v1',
    environment: ENV,
    checked_at: nowISO(),
    elapsed_ms: elapsed,
    healthy,
    services: { arkship, surrogate },
  };
}

function buildManifest(rootDir) {
  const allowedExts = new Set(['.js', '.css', '.html', '.json', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.ico', '.map']);
  const result = {};

  function walk(dir) {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const e of entries) {
      const full = path.join(dir, e.name);
      if (e.isDirectory()) {
        walk(full);
      } else if (e.isFile()) {
        const ext = path.extname(e.name).toLowerCase();
        if (!allowedExts.has(ext)) continue;
        const rel = path.relative(rootDir, full);
        const hash = safeHash(full);
        const size = safeStat(full);
        if (hash) {
          result[rel] = { hash, size };
        }
      }
    }
  }

  if (fs.existsSync(rootDir)) {
    walk(rootDir);
  }
  return result;
}

async function run() {
  try {
    fs.mkdirSync(OUT_DIR, { recursive: true });

    const status = await collectServices();
    const statusPath = path.join(OUT_DIR, 'status.json');
    fs.writeFileSync(statusPath, JSON.stringify(status, null, 2) + '\n', 'utf8');
    console.log(`✅ Wrote ${path.relative(process.cwd(), statusPath)}`);

    // Manifest from project build/dist output (if present). Default to OUT_DIR parent's build/dist/static or similar.
    const possibleRoots = [
      path.join(OUT_DIR, '..', '..'), // repo root if OUT_DIR is dist/discover
      path.join(process.cwd(), 'build'),
      path.join(process.cwd(), 'dist'),
      path.join(process.cwd(), 'public'),
    ].filter(p => fs.existsSync(p));

    const manifestRoot = possibleRoots.find(p => fs.readdirSync(p).length > 0) || OUT_DIR;
    const manifest = buildManifest(manifestRoot);
    const manifestPath = path.join(OUT_DIR, 'manifest.json');
    fs.writeFileSync(manifestPath, JSON.stringify(manifest, Object.keys(manifest).sort(), 2) + '\n', 'utf8');
    console.log(`✅ Wrote ${path.relative(process.cwd(),
