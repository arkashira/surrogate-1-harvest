# airship / frontend

## Implementation Plan — Frontend-safe `airship discover` orchestrator

**Goal (≤2h):** Ship a deterministic, CDN-only status snapshot + asset manifest generator so the frontend can render health/state without SSR/backend calls.

### What I’m shipping
1. CLI orchestrator: `bin/airship-discover` (Bash, safe for cron/Mac)
2. Outputs (committed to repo or artifact dir):
   - `dist/status-snapshot.json` — deterministic service health + state
   - `dist/asset-manifest.json` — CDN-safe asset paths + integrity
3. Frontend adapter: `src/frontend/lib/use-airship-status.js` — lightweight loader that fetches the CDN snapshot/manifest (no SSR/backend required).

### Why this scope
- Fits <2h: one orchestrator script + one small frontend module + tests.
- Aligns with past patterns:
  - Mac=CLI rule + heavy compute remote: Mac only runs orchestrator; no local servers.
  - CDN bypass: status snapshot is static and cacheable; frontend fetches via CDN.
  - Script safety: Bash shebang, executable, SHELL=/bin/bash friendly.
- Incremental value: unblocks frontend health/state rendering without backend/SSR changes.

---

### Implementation Steps

#### 1) Create orchestrator script
- Path: `bin/airship-discover`
- Responsibilities:
  - Detect environment (local/docker/CI)
  - Query service endpoints (Arkship 8000/8001) with short timeouts
  - Produce deterministic JSON snapshot (sorted keys, stable timestamps)
  - Generate asset manifest from known build outputs
  - Write to `dist/` with atomic rename
- Safety:
  - `#!/usr/bin/env bash`
  - `set -euo pipefail`
  - Use `timeout` for endpoint checks
  - Log to stderr; exit non-zero on fatal failure

#### 2) Add deterministic snapshot schema
- Fields:
  - `generatedAt` (ISO)
  - `services` (sorted by name)
    - `name`, `status` (`up`/`down`/`degraded`), `latencyMs`, `endpoint`, `checks[]`
  - `state` (minimal inferred state: `healthy`/`degraded`/`unhealthy`)
  - `version` (git short hash if available)

#### 3) Add asset manifest schema
- Fields:
  - `generatedAt`
  - `assets` (sorted by type+name)
    - `type` (`js`/`css`/`image`/`font`)
    - `path` (CDN-relative)
    - `integrity` (sha384 if available)
    - `size` (bytes)

#### 4) Frontend loader
- Path: `src/frontend/lib/use-airship-status.js`
- Exports:
  - `loadStatusSnapshot(url)` — fetches snapshot, validates minimal shape, returns parsed object
  - `loadAssetManifest(url)` — fetches manifest, returns assets map
- Behavior:
  - Uses `fetch` with cache-control awareness
  - Falls back to local `dist/` if fetch fails (for local dev)
  - No SSR: safe to call in browser only

#### 5) Add npm scripts + docs
- `npm run discover` — runs `bin/airship-discover`
- `npm run discover:ci` — runs with CI-friendly flags (no color, strict errors)
- Update README snippet with usage and cron example (SHELL=/bin/bash)

#### 6) Tests & validation
- Quick smoke test script: `scripts/test-discover.sh`
  - Runs orchestrator
  - Validates JSON schema (minimal jq checks)
  - Ensures dist files exist and are non-empty

---

### Code Snippets

#### bin/airship-discover
```bash
#!/usr/bin/env bash
# bin/airship-discover
# Orchestrator: produce static status snapshot + asset manifest (CDN-safe).
# Usage: ./bin/airship-discover [--output-dir dist] [--no-color]

set -euo pipefail

# Config defaults
OUTPUT_DIR="${1:-dist}"
NO_COLOR="${NO_COLOR:-}"
TIMEOUT_SECONDS=3

# Helpers
log() {
  if [ -z "${NO_COLOR:-}" ] && [ -t 1 ]; then
    printf '\033[34m[discover]\033[0m %s\n' "$*" >&2
  else
    printf '[discover] %s\n' "$*" >&2
  fi
}

fail() {
  if [ -z "${NO_COLOR:-}" ] && [ -t 1 ]; then
    printf '\033[31m[discover] ERROR:\033[0m %s\n' "$*" >&2
  else
    printf '[discover] ERROR: %s\n' "$*" >&2
  fi
  exit 1
}

# Ensure output dir
mkdir -p "${OUTPUT_DIR}"

# Detect version
VERSION="dev"
if git rev-parse --git-dir >/dev/null 2>&1; then
  VERSION="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
fi

# Service checks (sorted names for determinism)
check_service() {
  local name="$1"
  local url="$2"
  local start
  start="$(date +%s%3N)" || start="$(date +%s)000"
  local status="down"
  local latency=0
  local detail=""

  if response=$(timeout "${TIMEOUT_SECONDS}" curl -s -o /dev/null -w "%{http_code}" "${url}" 2>/dev/null); then
    latency=$(( $(date +%s%3N) - start ))
    if [ "${response}" -ge 200 ] && [ "${response}" -lt 400 ]; then
      status="up"
    else
      status="degraded"
      detail="http=${response}"
    fi
  else
    latency=$(( $(date +%s%3N) - start ))
    detail="timeout or unreachable"
  fi

  printf '{"name":"%s","status":"%s","latencyMs":%s,"endpoint":"%s","detail":"%s"}' \
    "$(echo "${name}" | sed 's/"/\\"/g')" \
    "${status}" \
    "${latency}" \
    "$(echo "${url}" | sed 's/"/\\"/g')" \
    "$(echo "${detail}" | sed 's/"/\\"/g')"
}

# Run checks (deterministic order)
ARkship_status=$(check_service "Arkship-Platform" "http://localhost:8000/health" || true)
Surrogate_status=$(check_service "Surrogate-AI" "http://localhost:8001/health" || true)

# Build services list (sorted)
services_json=$(printf '[%s,%s]' "${ARkship_status}" "${Surrogate_status}" | jq -S '.' 2>/dev/null || printf '[%s,%s]' "${ARkship_status}" "${Surrogate_status}")

# Determine overall state
overall="healthy"
if echo "${services_json}" | grep -q '"status":"down"'; then
  overall="unhealthy"
elif echo "${services_json}" | grep -q '"status":"degraded"'; then
  overall="degraded"
fi

generated_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%SZ")

# Status snapshot
snapshot=$(jq -n \
  --arg generatedAt "${generated_at}" \
  --arg version "${VERSION}" \
  --arg state "${overall}" \
  --argjson services "${services_json}" \
  '{
    generatedAt: $generatedAt,
    version: $version,
    state: $state,
    services: $services
  }')

snapshot_file="${OUTPUT_DIR}/status-snapshot.json"
tmp_snapshot="${snapshot_file}.tmp"
printf '%s\n' "${snapshot}" | jq -S '.' > "${tmp_snapshot}"
mv "${tmp_snapshot}" "${snapshot_file}"
log "Wrote status snapshot -> ${snapshot_file}"

# Asset manifest (simple, deterministic)
assets_json=$(jq -n '[]' \
  | jq '. + [{"type":"js","path":"/static/js/main.js","integrity":"","size":0}]' \
  | jq '. + [{"type":"css","path":"/static
