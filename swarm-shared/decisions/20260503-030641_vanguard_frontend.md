# vanguard / frontend

## 1. Diagnosis
- No deterministic CDN-first manifest exists at build time → training/UI ingest paths rely on runtime HF API calls (429 risk).
- Frontend has no integrity verification for dataset files (no SHA-256 or content-addressed checks) → nondeterministic training runs.
- Missing frontend build step to pin file list + ordering for a target date folder → reproducibility gaps.
- No local dev preview of dataset slices → frontend devs cannot validate ingestion without remote API.
- Hardcoded or ad-hoc file globbing in frontend code → breaks when HF repo structure changes.

## 2. Proposed change
Add a frontend build-time script and small UI module that:
- Runs `list_repo_tree` once (non-recursive) for a configured date folder and emits `public/manifest-{date}.json` (sorted, pinned).
- Embeds SHA-256 hashes for each file via CDN HEAD/etag (best-effort) or placeholder for integrity checks.
- Exposes a tiny React hook (`useDatasetManifest(date)`) that reads the local manifest and provides CDN URLs + integrity metadata.
- Scope: add `scripts/build-manifest.js`, `src/hooks/useDatasetManifest.js`, update `package.json` build script.

## 3. Implementation

### scripts/build-manifest.js
```bash
#!/usr/bin/env bash
# Build manifest for a date folder (non-recursive) and emit public/manifest-YYYY-MM-DD.json
# Usage: HF_TOKEN=... node scripts/build-manifest.js 2026-04-29
set -euo pipefail

DATE="${1:-$(date +%Y-%m-%d)}"
REPO="${HF_REPO:-axentx/surrogate-1}"
OUTDIR="public"
OUTFILE="${OUTDIR}/manifest-${DATE}.json"

mkdir -p "${OUTDIR}"

# Single non-recursive tree call (avoids pagination/429)
echo "Fetching tree for ${REPO} / ${DATE} (non-recursive)..."
FILES_JSON=$(curl -sSf \
  -H "Authorization: Bearer ${HF_TOKEN:-}" \
  "https://huggingface.co/api/datasets/${REPO}/tree?path=${DATE}&recursive=false")

# Extract paths and produce CDN URLs + best-effort size
echo "${FILES_JSON}" | node -e "
const fs = require('fs');
const items = JSON.parse(fs.readFileSync('/dev/stdin', 'utf8'));
const out = items
  .filter(x => x.type === 'file')
  .sort((a, b) => a.path.localeCompare(b.path))
  .map(x => ({
    path: x.path,
    cdn: \`https://huggingface.co/datasets/${REPO}/resolve/main/\${x.path}\`,
    size: x.size || null,
    // Note: ETag/sha256 not provided by tree API; can be fetched per-file if needed.
    // For strict integrity, run a one-off HEAD per file during manifest build.
    integrity: null
  }));
fs.writeFileSync('${OUTFILE}', JSON.stringify({ date: '${DATE}', repo: '${REPO}', generatedAt: new Date().toISOString(), count: out.length, files: out }, null, 2));
console.log('Wrote', out.length, 'entries to', '${OUTFILE}');
"
```

Make executable:
```bash
chmod +x scripts/build-manifest.js
```

### src/hooks/useDatasetManifest.js
```js
import { useEffect, useState } from 'react';

export function useDatasetManifest(date) {
  const [manifest, setManifest] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!date) {
      setLoading(false);
      return;
    }
    setLoading(true);
    fetch(`/manifest-${date}.json`)
      .then((r) => {
        if (!r.ok) throw new Error(`Failed to load manifest: ${r.status}`);
        return r.json();
      })
      .then((data) => {
        setManifest(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(err);
        setLoading(false);
      });
  }, [date]);

  return { manifest, loading, error };
}
```

### package.json (add build hook)
```json
{
  "scripts": {
    "build:manifest": "node scripts/build-manifest.js",
    "build": "npm run build:manifest && ...existing-build..."
  }
}
```

## 4. Verification
- Run locally: `HF_TOKEN=... npm run build:manifest 2026-04-29`
- Confirm `public/manifest-2026-04-29.json` exists, contains sorted `files[]` with `cdn` URLs, and count > 0.
- Start dev server and in browser console: `await (await fetch('/manifest-2026-04-29.json')).json()` — should return same structure.
- In a component: `const { manifest, loading } = useDatasetManifest('2026-04-29');` — should render file list with CDN links.
- Confirm no network calls to `api.huggingface.co` from the frontend when loading the manifest (only CDN fetches).
