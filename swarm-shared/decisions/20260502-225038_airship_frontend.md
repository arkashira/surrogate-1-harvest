# airship / frontend

## Final Implementation — Frontend-safe `airship discover` (merged + hardened)

**Single goal (≤2h):** Deterministic, CDN-cacheable status + asset manifest so the frontend can render health/state with zero backend/SSR.

---

### What ships
- CLI: `bin/airship-discover` (Bash, executable, portable)
- Outputs (committed or CI-uploaded to CDN):
  - `public/status/snapshot.json` — deterministic service/health + asset snapshot (timestamped, content-hashed)
  - `public/status/manifest.json` — CDN asset manifest (cache-bust hashes, sizes, integrity, routing hints)
- Frontend: `src/lib/airshipStatus.js` — tiny CDN-first loader with graceful fallbacks and React hook
- Package scripts + pre-build hook to ensure snapshot is fresh before frontend builds

---

### Why this satisfies constraints
- No backend/SSR: frontend fetches static JSON from CDN (relative paths).
- CDN-only after generation: bypasses API rate limits; long TTL safe via content-hash cache busting.
- Deterministic + cacheable: ETag-friendly (content-hash in snapshot + filenames), explicit `generatedAt` and `ttlSeconds`.
- <2h scope: focused on generation + frontend hydration; no infra changes.

---

### Implementation steps (ordered)

1. Create `bin/airship-discover`
   - Shebang, `set -euo pipefail`, check for `jq`/`curl`/`find`/`date`.
   - Discover services from `docker-compose.microservices.yml` (preferred) + known ports fallback.
   - Probe endpoints with short timeout (`--max-time 2`) for `/health` and known paths.
   - Enumerate frontend build assets, compute `sha256` hashes and sizes for cache-busting manifest.
   - Write `public/status/snapshot.json` + `public/status/manifest.json`.

2. Add frontend loader `src/lib/airshipStatus.js`
   - Fetch `/status/snapshot.json` and `/status/manifest.json` (relative, CDN-friendly).
   - Expose `loadAirshipStatus()`, `getStatusSnapshot()`, `getAssetManifest()`.
   - Graceful fallbacks: return minimal default snapshot on 404/network failure; do not throw in UI.
   - Optional React hook (`useAirshipStatus`) for quick integration.

3. Add npm scripts + pre-build hook
   - `"discover": "bin/airship-discover"`
   - `"prediscover": "mkdir -p public/status"`
   - Add `prebuild` hook to run `npm run discover` so builds consume fresh snapshot.

4. CI/CD (optional, within scope)
   - After generation, upload `public/status/` to CDN or commit if using repo-pages.
   - Set long cache TTLs + content-hash filenames for immutable assets.

---

### Code (production-ready)

#### `bin/airship-discover`
```bash
#!/usr/bin/env bash
# bin/airship-discover
# Generates static status snapshot + asset manifest for CDN consumption.
# Usage: bin/airship-discover [--out <dir>] (default: public/status)

set -euo pipefail

OUT_DIR="public/status"
if [[ "${1:-}" == "--out" && -n "${2:-}" ]]; then
  OUT_DIR="$2"
fi

mkdir -p "$OUT_DIR"

# ---- Requirements ----
for cmd in jq curl find date; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $cmd" >&2
    exit 1
  fi
done

TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
SCHEMA_STATUS="airship-discover/snapshot/v1"
SCHEMA_MANIFEST="airship-manifest/v1"

# ---- Service discovery ----
DISCOVERED_SERVICES=()
if [[ -f docker-compose.microservices.yml ]]; then
  # extract top-level service keys (robust to indentation)
  while IFS= read -r line; do
    DISCOVERED_SERVICES+=("$line")
  done < <(sed -nE 's/^[[:space:]]*([a-zA-Z0-9_-]+):.*/\1/p' docker-compose.microservices.yml)
fi

# Ensure known services are present even if compose parsing fails
for known in arkship surrogate; do
  if ! printf '%s\n' "${DISCOVERED_SERVICES[@]}" | grep -qx "$known"; then
    DISCOVERED_SERVICES+=("$known")
  fi
done

# ---- Endpoint probing ----
probe() {
  local url="$1"
  if curl -fs --max-time 2 "$url" >/dev/null 2>&1; then
    echo "up"
  else
    echo "down"
  fi
}

ENDPOINTS_JSON="[]"
for svc in "${DISCOVERED_SERVICES[@]}"; do
  case "$svc" in
    arkship)
      url="http://localhost:8000"
      health=$(probe "$url/health" || true)
      ENDPOINTS_JSON=$(echo "$ENDPOINTS_JSON" | jq --arg name "arkship" --arg url "$url" --arg health "$health" \
        '. + [{"name":$name,"url":$url,"health":$health,"type":"platform"}]')
      ;;
    surrogate)
      url="http://localhost:8001"
      health=$(probe "$url/health" || true)
      ENDPOINTS_JSON=$(echo "$ENDPOINTS_JSON" | jq --arg name "surrogate" --arg url "$url" --arg health "$health" \
        '. + [{"name":$name,"url":$url,"health":$health,"type":"ai"}]')
      ;;
    *)
      # generic probe on common ports
      found=0
      for port in 8000 8001 3000 8080; do
        url="http://localhost:${port}"
        health=$(probe "$url/health" || true)
        if [[ "$health" == "up" ]]; then
          ENDPOINTS_JSON=$(echo "$ENDPOINTS_JSON" | jq --arg name "$svc" --arg url "$url" --arg health "$health" \
            '. + [{"name":$name,"url":$url,"health":$health,"type":"unknown"}]')
          found=1
          break
        fi
      done
      if [[ $found -eq 0 ]]; then
        # include service as down for visibility
        ENDPOINTS_JSON=$(echo "$ENDPOINTS_JSON" | jq --arg name "$svc" \
          '. + [{"name":$name,"url":null,"health":"down","type":"unknown"}]')
      fi
      ;;
  esac
done

# ---- Asset manifest ----
ASSETS_JSON="[]"
# Prefer build output dirs; fallback to public/
ASSET_DIR=""
for d in dist build public; do
  if [[ -d "$d" ]]; then
    ASSET_DIR="$d"
    break
  fi
done

if [[ -n "$ASSET_DIR" ]]; then
  while IFS= read -r -d '' f; do
    rel="${f#./}"
    hash="$(sha256sum "$f" 2>/dev/null | awk '{print $1}' | head -c 12 || echo "unknown")"
    size="$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0)"
    ASSETS_JSON=$(echo "$ASSETS_JSON" | jq --arg path "$rel" --arg hash "$hash" --argjson size "$size" \
      '. + [{"path":$path,"hash":$hash,"size":$size}]')
  done < <(find "$ASSET_DIR" -type f \( -name "*.js" -o -name "*.css" -o -name "*.json" -o -name "*.svg" -o -name "*.png" -o -name "*.webp" \) -print0 2>/dev/null | head -z -50)
fi

# ---- Build snapshot.json ----
STATUS_JSON=$(jq -n \
  --arg ts "$TIMESTAMP" \
  --arg schema "$SCHEMA_STATUS" \
  --argjson endpoints "$ENDPOINTS_JSON" \
  --argjson assets "$ASSETS_JSON" \
  '{
    generatedAt: $ts,
    schema: $schema,
    endpoints
