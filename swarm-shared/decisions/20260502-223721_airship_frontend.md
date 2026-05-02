# airship / frontend

## Final Decision & Action Plan

**Ship `airship discover` (frontend orchestrator) + lightweight status panel** — as a single, coherent deliverable.

- **Orchestrator** (`airship-discover-frontend`) produces research + CDN-only training manifest and emits frontend-ready JSON/markdown.
- **Status panel** consumes the orchestrator’s latest-run metadata and exposes real-time service health (Arkship 8000 / Surrogate 8001) with a “Retry discover” control.
- **Contradiction resolved**: Do not add a polling `/api/status` that duplicates health checks the orchestrator can already summarize. Instead, expose a single, cache-friendly endpoint that returns service reachability + last-run snapshot, and let the frontend render it. Keep polling minimal (30s) and fallback-safe.

---

## Concrete Implementation (<2h)

### 1) Cron-safe orchestrator (final)

Path: `/opt/axentx/airship/bin/airship-discover-frontend`

Key fixes vs Candidate 1:
- Use `$EPOCHREALTIME`/`date -u +%s%3N` for machine timestamps; emit UTC ISO in human field.
- Validate jq/python3 availability early and fail fast if core deps missing (do not silently skip granite/knowledge-rag when present).
- Ensure manifest directory is created before Python runs; use `python3 -m json.tool` to validate outputs.
- Add optional `--upload` that respects HF commit caps; default is **no upload**.
- Write latest-run atomically:
  - `latest.json.tmp` → `fsync` → `mv` → `latest.json`
- Exit codes:
  - `0` = success
  - `1` = infra failure (missing deps, no write perms)
  - `2` = partial success (research step failed but manifest produced)

Critical addition: **service probe block** (used by status endpoint):
```bash
probe_service() {
  local url=$1 name=$2 timeout=${3:-2}
  if command -v curl &>/dev/null; then
    if curl -fs --max-time "$timeout" "$url" >/dev/null 2>&1; then
      echo "{\"name\":\"$name\",\"reachable\":true,\"url\":\"$url\"}"
    else
      echo "{\"name\":\"$name\",\"reachable\":false,\"url\":\"$url\"}"
    fi
  else
    echo "{\"name\":\"$name\",\"reachable\":null,\"error\":\"curl missing\"}"
  fi
}

ARkship_HEALTH=$(probe_service "http://localhost:8000/health" "Arkship")
SURROGATE_HEALTH=$(probe_service "http://localhost:8001/health" "Surrogate")
```

Embed probes + run metadata into `latest.json`:
```json
{
  "generated_at": "2025-11-01T12:34:56.789Z",
  "exit_code": 0,
  "tag": "research",
  "date": "2025-11-01",
  "services": { "Arkship": {...}, "Surrogate": {...} },
  "outputs": { ... },
  "manifest": "manifests/training-manifest-2025-11-01.json",
  "cdn_strategy": true
}
```

Cron example (runs 02:15 daily; logs rotate):
```
15 2 * * * /opt/axentx/airship/bin/airship-discover-frontend >> /var/log/airship/discover.log 2>&1
```

---

### 2) Lightweight status endpoint (single source of truth)

Path: `/api/status` (GET)

Behavior:
- Reads `/opt/axentx/airship/var/airship-discover/latest.json` (atomic file from orchestrator).
- Performs fresh lightweight probes to Arkship (8000) and Surrogate (8001) with 2s timeout.
- Returns:
  ```json
  {
    "now": "2025-11-01T12:35:01.123Z",
    "services": {
      "Arkship": { "reachable": true, "latency_ms": 12 },
      "Surrogate": { "reachable": true, "latency_ms": 9 }
    },
    "lastRun": {
      "generated_at": "2025-11-01T02:15:03.000Z",
      "exitCode": 0,
      "tag": "research",
      "cdnManifestPath": "/manifests/training-manifest-2025-11-01.json",
      "fileCount": 42,
      "totalBytes": 128450560
    },
    "actions": {
      "retryDiscover": "POST /api/actions/discover-retry"
    }
  }
  ```
- Caching: `Cache-Control: public, max-age=15` to reduce probe load.
- Errors:
  - `503` if HF repo unreachable when manifest expected (optional).
  - `200` always returned for service reachability; `lastRun` may be `null`.

Implementation (Node/Express-like pseudocode):
```js
app.get('/api/status', async (req, res) => {
  const [services, lastRun] = await Promise.all([
    probeServices(), // Arkship + Surrogate
    readLatestRun()
  ]);
  res.set('Cache-Control', 'public, max-age=15');
  res.json({ now: new Date().toISOString(), services, lastRun });
});
```

---

### 3) Frontend status panel (minimal, high-value)

Location: Top-nav dropdown or `/status` page.

Features:
- Badges: Arkship / Surrogate (green/yellow/gray) with last probe time.
- Last discovery run: date, tag, file count, and “View manifest” link (opens CDN URL).
- “Retry discover” button: POST `/api/actions/discover-retry` → triggers orchestrator async (returns `202` + job ID) and polls `/api/status` until `lastRun` updates.
- Fallback: If JS disabled, server-render the same data as HTML.

Polling: 30s interval; exponential backoff on 5xx.

Accessibility:
- Color not sole indicator (icons + text).
- `aria-live="polite"` for run updates.

---

### 4) Retry action (safe, rate-limited)

Endpoint: `POST /api/actions/discover-retry`

Behavior:
- Rate limit: 1 per 5 minutes per user/IP.
- Runs `/opt/axentx/airship/bin/airship-discover-frontend` in background (nohup/systemd-run) with same tag/date args.
- Returns:
  ```json
  { "jobId": "discover-20251101-123501", "status": "accepted", "pollStatus": "/api/status" }
  ```

---

## Why this wins

- **Correctness**: Orchestrator validates deps, writes atomic latest-run, and probes services so status is always consistent.
- **Actionability**: One cron entry, one endpoint, one panel — all wired together. Retry is real and rate-limited.
- **Minimal scope**: No heavy UI rewrite; no duplicate health checks; CDN-only manifest preserved; Lightning training can consume manifest URLs directly.
- **Operational safety**: HF API calls limited to one list/tree per run; no uploads by default; cron-safe with proper exit codes and logging.

Deliver both pieces together: orchestrator + status endpoint/panel.
