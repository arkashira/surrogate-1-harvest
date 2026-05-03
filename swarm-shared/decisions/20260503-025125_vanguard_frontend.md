# vanguard / frontend

## 1. Diagnosis
- No deterministic frontend build pipeline → no HMR, no asset hashing, fragile runtime resolution; developers cannot iterate locally with confidence.
- Missing mount point and entrypoint → no clear boundary between orchestration host and browser runtime; cannot render or iterate without manual server setup.
- No asset manifest → frontend cannot reliably resolve dataset files; every dev cycle risks 404s or stale references and exposes the app to HF API rate limits.
- No CDN-first data strategy at the frontend layer → runtime fetches hit `/api/` endpoints and risk 429s instead of using `resolve/main/` CDN bypass.
- No lightweight dev server or build script → forces ad-hoc static serving and manual reloads, slowing frontend feedback loops.

## 2. Proposed change
Add a minimal, deterministic frontend build/dev entrypoint and asset manifest generator:
- Create `frontend/index.html` (mount point)
- Create `frontend/src/main.js` (app entry)
- Create `scripts/build-frontend.sh` (build + manifest generator)
- Create `ops/run` (deterministic ops launcher for frontend dev/build)
- Update `.gitignore` for build outputs

Scope: new files + one-line cron/shell hygiene where needed.

## 3. Implementation

```bash
# Ensure project root
cd /opt/axentx/vanguard
```

### frontend/index.html
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Vanguard</title>
  <link rel="icon" href="/favicon.svg" />
  <!-- HMR client in dev -->
  <script type="module" src="/src/main.js"></script>
</head>
<body>
  <div id="app"></div>
</body>
</html>
```

### frontend/src/main.js
```javascript
// Minimal app entry with CDN-first dataset resolution
const API_ROOT = import.meta.env.VITE_API_ROOT || '';
const CDN_ROOT = import.meta.env.VITE_CDN_ROOT || 'https://huggingface.co/datasets';

async function loadManifest() {
  // Build-time generated; falls back to runtime fetch only in dev
  try {
    const res = await fetch('/assets/manifest.json', { cache: 'no-store' });
    if (!res.ok) throw new Error('no manifest');
    return res.json();
  } catch {
    return { files: [], generated: null };
  }
}

function createFileUrl(repo, path) {
  // CDN bypass: use resolve/main/ (no Authorization header, avoids /api/ rate limits)
  return `${CDN_ROOT}/${repo}/resolve/main/${path}`;
}

async function mount() {
  const app = document.getElementById('app');
  const manifest = await loadManifest();

  app.innerHTML = `
    <main style="font-family: system-ui; padding: 1rem;">
      <h1>Vanguard</h1>
      <p>Generated: ${manifest.generated || 'N/A'}</p>
      <ul>
        ${manifest.files.map(f => `
          <li>
            <a href="${createFileUrl(f.repo || 'datasets', f.path)}" target="_blank" rel="noopener">
              ${f.name || f.path}
            </a>
          </li>
        `).join('')}
      </ul>
    </main>
  `;
}

mount();
```

### scripts/build-frontend.sh
```bash
#!/usr/bin/env bash
set -euo pipefail

# Build frontend assets and generate CDN-first manifest.
# Usage: scripts/build-frontend.sh [--dev]
#
# Notes:
# - Uses HF CDN bypass (resolve/main/) to avoid /api/ rate limits.
# - Manifest is intended to be consumed by frontend at runtime.
# - In production, embed file list via CI; in dev, allow runtime fallback.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${ROOT}/frontend"
DIST_DIR="${ROOT}/dist"
MANIFEST_DIR="${DIST_DIR}/assets"
MANIFEST_FILE="${MANIFEST_DIR}/manifest.json"

DEV_MODE=0
for arg in "$@"; do
  case "$arg" in
    --dev) DEV_MODE=1 ;;
  esac
done

echo "Building frontend (dev=${DEV_MODE})..."

# Ensure dirs
mkdir -p "${MANIFEST_DIR}"

# Simple "build": copy frontend to dist (in real usage, swap in Vite/Parcel/etc.)
rm -rf "${DIST_DIR}"
cp -r "${FRONTEND_DIR}" "${DIST_DIR}/frontend-src"
cp "${FRONTEND_DIR}/index.html" "${DIST_DIR}/index.html"

# Generate manifest (minimal example).
# In production, this should be populated by an earlier ingestion step
# (e.g., list_repo_tree saved to JSON) and embedded here.
# We use CDN URLs to avoid HF API auth/rate limits at runtime.

cat > "${MANIFEST_FILE}" <<EOF
{
  "generated": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "files": [
    {
      "name": "example-dataset-001.parquet",
      "path": "example-org/example-dataset/raw/main/example-dataset-001.parquet",
      "repo": "example-org/example-dataset",
      "cdn_url": "https://huggingface.co/datasets/example-org/example-dataset/resolve/main/example-dataset-001.parquet"
    }
  ],
  "cdn_root": "https://huggingface.co/datasets",
  "note": "In production, replace this file with an ingestion-generated manifest listing exact date-partitioned folders."
}
EOF

echo "Manifest written to ${MANIFEST_FILE}"

# If dev mode, also start a minimal static server (python3 available on most dev boxes)
if [[ "${DEV_MODE}" -eq 1 ]]; then
  echo "Starting dev server on http://localhost:8080"
  cd "${DIST_DIR}"
  python3 -m http.server 8080
fi
```

Make executable:
```bash
chmod +x scripts/build-frontend.sh
```

### ops/run
```bash
#!/usr/bin/env bash
# Deterministic ops launcher for vanguard (frontend focus).
# Usage: ops/run <command> [args...]
#
# Commands:
#   frontend:build   - build frontend assets + manifest
#   frontend:dev     - build + start dev server
#
# Notes:
# - Enforces Bash and executable semantics to avoid wrapper/cron issues.
# - Sets SHELL for cron compatibility.

set -euo pipefail
export SHELL=/bin/bash

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

cmd="${1:-}"
shift || true

case "${cmd}" in
  frontend:build)
    exec "${ROOT}/scripts/build-frontend.sh" "$@"
    ;;
  frontend:dev)
    exec "${ROOT}/scripts/build-frontend.sh" --dev
    ;;
  *)
    echo "Unknown command: ${cmd}" >&2
    echo "Available: frontend:build, frontend:dev" >&2
    exit 1
    ;;
esac
```

Make executable:
```bash
chmod +x ops/run
```

### .gitignore additions
```gitignore
/dist/
/node_modules/
.env.local
```

## 4. Verification

1. Build and check manifest:
   ```bash
   ops/run frontend:build
   cat dist/assets/manifest.json
   ```
   Expect valid JSON with `generated` timestamp and `files` array containing CDN URLs (`resolve/main/`).

2. Start dev server and verify mount:
   ```bash
   # In one terminal
   ops/run frontend:dev
   # In another (or browser)
   curl -s http://localhost:8080 | grep -i vanguard
   curl -s http://localhost:8080/assets/manifest.json | jq .
   ```
   Expect HTML with mount point and manifest served.

3. Confirm CDN-first URLs:
   - Manifest entries should use `https://huggingface.co/datasets/.../resolve/main/...` (no `/api/`).
   - Opening a file link in browser should fetch via CDN (no Authorization header required).

4. Cron/shell hygiene (prevent known wrapper failures):
   - Ensure any cron entries invoke
