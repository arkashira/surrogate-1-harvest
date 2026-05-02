# airship / frontend

## Final Consolidated Implementation (Best-of + Corrected + Actionable)

**Goal (≤2h):** Ship a deterministic, CDN-cacheable status snapshot + asset manifest so the Arkship frontend can render health/state with **zero SSR, zero backend calls, zero secrets**.

**Why this wins:**
- Unblocks frontend health rendering immediately.
- No backend/auth changes; aligns with prior patterns (orchestration on Mac, heavy compute elsewhere).
- Combines Candidate 1’s robust CLI/fallbacks with Candidate 2’s HF CDN bypass and typed frontend, while fixing contradictions (location, manifest shape, fallback behavior).

---

## 1) CLI orchestrator — `bin/airship-discover`

Location: project root `bin/` (consistent with Candidate 1; avoids inventing `scripts/`). Single file, no Node runtime required for the orchestrator.

```bash
#!/usr/bin/env bash
# bin/airship-discover
# Orchestrates discovery for Arkship + Surrogate and emits CDN-safe snapshots.
#
# Usage:
#   bash bin/airship-discover [--output <dir>] [--stamp <YYYYmmdd-HHMMSS>]
#
# Cron guidance:
#   SHELL=/bin/bash
#   */5 * * * * /bin/bash /opt/axentx/airship/bin/airship-discover >> /var/log/airship-discover.log 2>&1

set -euo pipefail

# Defaults
BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="${BASE_DIR}/dist"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
TIMEOUT=10
HF_DATASET_ROOT="${HF_DATASET_ROOT:-}"

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --output) OUTPUT_DIR="$2"; shift 2 ;;
    --stamp)  STAMP="$2"; shift 2 ;;
    --help|-h) echo "Usage: $0 [--output <dir>] [--stamp <YYYYmmdd-HHMMSS>]"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

mkdir -p "${OUTPUT_DIR}"

ARKSHIP_HEALTH_URL="http://localhost:8000/health"
SURROGATE_HEALTH_URL="http://localhost:8001/health"
ARKSHIP_READY_URL="http://localhost:8000/ready"
SURROGATE_READY_URL="http://localhost:8001/ready"

# Fetch helper with timeout + graceful failure
fetch_json() {
  local url="$1"
  local out="$2"
  if curl -fs --max-time "${TIMEOUT}" -H "Accept: application/json" "${url}" > "${out}" 2>/dev/null; then
    return 0
  else
    echo '{"status":"unreachable","error":"fetch_failed"}' > "${out}"
    return 1
  fi
}

# Temporary files
ARK_HEALTH_TMP="$(mktemp)"
SUR_HEALTH_TMP="$(mktemp)"
ARK_READY_TMP="$(mktemp)"
SUR_READY_TMP="$(mktemp)"

cleanup() {
  rm -f "${ARK_HEALTH_TMP}" "${SUR_HEALTH_TMP}" "${ARK_READY_TMP}" "${SUR_READY_TMP}"
}
trap cleanup EXIT

# Collect
fetch_json "${ARKSHIP_HEALTH_URL}" "${ARK_HEALTH_TMP}" || true
fetch_json "${SURROGATE_HEALTH_URL}" "${SUR_HEALTH_TMP}" || true
fetch_json "${ARKSHIP_READY_URL}" "${ARK_READY_TMP}" || true
fetch_json "${SURROGATE_READY_URL}" "${SUR_READY_TMP}" || true

# Build snapshot
SNAPSHOT="${OUTPUT_DIR}/discover-${STAMP}.json"
cat > "${SNAPSHOT}" <<EOF
{
  "snapshot": {
    "generated_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "stamp": "${STAMP}",
    "sources": {
      "arkship": {
        "health_url": "${ARKSHIP_HEALTH_URL}",
        "ready_url": "${ARKSHIP_READY_URL}",
        "health": $(cat "${ARK_HEALTH_TMP}"),
        "ready": $(cat "${ARK_READY_TMP}")
      },
      "surrogate": {
        "health_url": "${SURROGATE_HEALTH_URL}",
        "ready_url": "${SURROGATE_READY_URL}",
        "health": $(cat "${SUR_HEALTH_TMP}"),
        "ready": $(cat "${SUR_READY_TMP}")
      }
    }
  }
}
EOF

# Latest copy
LATEST="${OUTPUT_DIR}/latest.json"
cp "${SNAPSHOT}" "${LATEST}"

# Status.json (canonical name expected by frontend)
STATUS="${OUTPUT_DIR}/status.json"
cp "${SNAPSHOT}" "${STATUS}"

# Optional HF dataset listing (CDN bypass)
hf_files=()
if [[ -n "${HF_DATASET_ROOT}" && -d "${HF_DATASET_ROOT}" ]]; then
  while IFS= read -r -d '' f; do
    rel="${f#${BASE_DIR}/}"
    hf_files+=("${rel}")
  done < <(find "${HF_DATASET_ROOT}" -type f -print0 2>/dev/null)
fi

# Manifest for CDN assets
MANIFEST="${OUTPUT_DIR}/manifest.json"
cat > "${MANIFEST}" <<EOF
{
  "generated_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "stamp": "${STAMP}",
  "snapshot": "discover-${STAMP}.json",
  "latest": "latest.json",
  "status": "status.json",
  "integrity": {
    "discover": "$(sha256sum "${SNAPSHOT}" | awk '{print $1}')",
    "latest": "$(sha256sum "${LATEST}" | awk '{print $1}')",
    "status": "$(sha256sum "${STATUS}" | awk '{print $1}')"
  },
  "hf_dataset_files": $(printf '%s\n' "${hf_files[@]}" | jq -R . | jq -s . 2>/dev/null || echo '[]'),
  "cache_control": "public, max-age=60, stale-while-revalidate=300"
}
EOF

echo "✅ Discovery snapshot written to ${SNAPSHOT}"
echo "✅ Latest at ${LATEST}"
echo "✅ Status at ${STATUS}"
echo "✅ Manifest at ${MANIFEST}"
```

Make executable:
```bash
chmod +x bin/airship-discover
```

Package.json script (for convenience):
```json
"scripts": {
  "discover": "bash bin/airship-discover"
}
```

---

## 2) Node helper (optional) — `scripts/build-status.js`

If you want a Node-based normalizer (e.g., to merge multiple runs or produce minified outputs), add this. It is **optional**; the Bash orchestrator is sufficient for the ≤2h goal.

```js
// scripts/build-status.js
// Reads dist/discover-*.json and writes dist/status.json + manifest augmentations if needed.
// Usage: node scripts/build-status.js [--input dist/discover-YYYYmmdd-HHMMSS.json] [--output dist/status.json]

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE = path.resolve(__dirname, '..);

function readJson(p) {
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

function writeJson(p, obj) {
  fs.writeFileSync(p, JSON.stringify(obj, null, 2) + '\n');
}

function main() {
  const args = process.argv.slice(2);
  let input = null;
  let output = path.join(BASE, 'dist/status.json');

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--input' && args[i + 1]) input = args[++i];
    if (args[i] === '--output' && args[i + 1]) output = args[++i];
  }

  if (!input) {
    // pick latest discover-*.json in dist
    const files = fs.readdirSync(path.join(BASE, 'dist')).filter((f) => f.startsWith('discover-') && f.endsWith('.
