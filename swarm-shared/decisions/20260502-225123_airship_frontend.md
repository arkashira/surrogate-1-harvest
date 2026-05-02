# airship / frontend

## Final Implementation (merged + reconciled)

**Goal**: A deterministic, CDN-cacheable `airship discover` orchestrator that produces a status snapshot + asset manifest so the frontend can render global health without SSR/backend calls. Fits ≤2h.

**Decisions that resolve contradictions in favor of correctness + actionability**
- Single executable orchestrator placed at `/opt/axentx/airship/bin/airship-discover` (not `scripts/`) so it’s on PATH and callable from CI/cron.
- Output directory: `public/airship/` (not bare `dist/`) so files are immediately served by the static frontend and CDN without extra routing.
- Atomic writes via `mv tmp.json file.json` to prevent partial reads during concurrent runs.
- Use `curl` + short timeouts + fallback to cached snapshot if unreachable (keeps site usable when services are down).
- Include frontend loader as `public/airship/airship-loader.js` (UMD) so the UI can hydrate state immediately from CDN.
- Deterministic schema: `status.json` (health snapshot) + `manifest.json` (CDN asset map with hashes and cache-control). No mixed-schema writes.
- CI step: add `npm run discover` (or direct script call) to build pipeline so `public/airship/` is published to CDN on deploy.

---

## 1) Orchestrator: `/opt/axentx/airship/bin/airship-discover`

```bash
#!/usr/bin/env bash
# airship-discover
# Produces a deterministic status snapshot + asset manifest for CDN.
# Usage: ./bin/airship-discover [--out-dir public/airship]
#
# Requirements: curl, jq, sha256sum
# Set SHELL=/bin/bash in crontab if scheduled.

set -euo pipefail

OUT_DIR="${2:-public/airship}"
SERVICES=(
  "Arkship-UI:http://localhost:3000"
  "Arkship-API:http://localhost:8000"
  "Surrogate-AI:http://localhost:8001"
)

# Optional extra checks via env (one per line "Name:url")
EXTRA_SERVICES="${EXTRA_SERVICES:-}"
if [[ -n "$EXTRA_SERVICES" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    SERVICES+=("$line")
  done <<< "$EXTRA_SERVICES"
fi

mkdir -p "$OUT_DIR"

timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
epoch=$(date -u +"%s")

# Health check helper
check_service() {
  local name="$1"
  local url="$2"
  local status="unknown"
  local latency_ms=-1
  local version="n/a"
  local detail=""

  local start_ts
  start_ts=$(date +%s%3N 2>/dev/null || echo "$(($(date +%s)*1000))")
  local code
  local headers
  if headers=$(curl -sS --max-time 5 -D - "$url" -o /dev/null 2>/dev/null); then
    local end_ts
    end_ts=$(date +%s%3N 2>/dev/null || echo "$(($(date +%s)*1000))")
    latency_ms=$((end_ts - start_ts))
    code=$(echo "$headers" | grep -i "^HTTP/" | awk '{print $2}' | head -1)
    if [[ "$code" =~ ^2 ]]; then
      status="healthy"
    else
      status="degraded"
      detail="HTTP $code"
    fi

    # Try common version endpoints
    for probe in "/version" "/api/version" "/api/health" "/healthz"; do
      local vurl="${url%/}$probe"
      local vresp
      if vresp=$(curl -sS --max-time 3 "$vurl" 2>/dev/null); then
        local vcandidate
        vcandidate=$(echo "$vresp" | jq -r '.version // .build // .tag // empty' 2>/dev/null || true)
        if [[ -n "$vcandidate" && "$vcandidate" != "null" ]]; then
          version="$vcandidate"
          break
        fi
      fi
    done
  else
    status="down"
    detail="unreachable"
  fi

  jq -n \
    --arg name "$name" \
    --arg url "$url" \
    --arg status "$status" \
    --arg version "$version" \
    --arg detail "$detail" \
    --argjson latency "$latency_ms" \
    --arg ts "$timestamp" \
    '{
      name: $name,
      url: $url,
      status: $status,
      version: $version,
      detail: $detail,
      latency_ms: $latency,
      checked_at: $ts
    }'
}

# Run checks
checks=()
for svc in "${SERVICES[@]}"; do
  IFS=: read -r name url <<< "$svc"
  checks+=("$(check_service "$name" "$url")")
done

status_snapshot=$(jq -n \
  --arg ts "$timestamp" \
  --argjson epoch "$epoch" \
  --argjson checks "$(printf '%s\n' "${checks[@]}" | jq -s '.')" \
  '{
    generated_at: $ts,
    epoch: $epoch,
    services: $checks,
    summary: {
      healthy: ($checks | map(select(.status == "healthy")) | length),
      degraded: ($checks | map(select(.status == "degraded")) | length),
      down: ($checks | map(select(.status == "down")) | length)
    }
  }')

# Atomic write
echo "$status_snapshot" > "$OUT_DIR/status.json.tmp"
mv "$OUT_DIR/status.json.tmp" "$OUT_DIR/status.json"

# Build asset manifest for CDN (include status.json + any static assets)
manifest_assets=()
if [[ -f "$OUT_DIR/status.json" ]]; then
  hash=$(sha256sum "$OUT_DIR/status.json" | awk '{print $1}')
  size=$(stat -c%s "$OUT_DIR/status.json" 2>/dev/null || stat -f%z "$OUT_DIR/status.json" 2>/dev/null || echo 0)
  manifest_assets+=("$(jq -n \
    --arg path "status.json" \
    --arg hash "$hash" \
    --arg size "$size" \
    --arg cache_control "public, max-age=60, stale-while-revalidate=300" \
    '{
      path: $path,
      hash: $hash,
      size: ($size | tonumber),
      cache_control: $cache_control,
      url: ("/airship/status.json?v=" + $hash)
    }')")
fi

# Include frontend build outputs if present (common locations)
for static_dir in "../frontend/dist" "./public" "../frontend/build" "."; do
  if [[ -d "$static_dir" ]]; then
    while IFS= read -r -d '' f; do
      rel=$(realpath --relative-to="$OUT_DIR" "$f" 2>/dev/null || echo "$f")
      hash=$(sha256sum "$f" | awk '{print $1}')
      size=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0)
      ext="${f##*.}"
      case "$ext" in
        js|css|html|json|svg|png|jpg|jpeg|webp|ico)
          mime="application/octet-stream"
          case "$ext" in
            js) mime="application/javascript";;
            css) mime="text/css";;
            html) mime="text/html";;
            json) mime="application/json";;
            svg) mime="image/svg+xml";;
            png) mime="image/png";;
            jpg|jpeg) mime="image/jpeg";;
            webp) mime="image/webp";;
            ico) mime="image/x-icon";;
          esac

          manifest_assets+=("$(jq -n \
            --arg path "$rel" \
            --arg hash "$hash" \
            --arg size "$size" \
            --arg mime "$mime" \
            --arg cache_control "public, max-age=31536000, immutable" \
            '{
              path
