# airship / frontend

## Final synthesized implementation (best of both proposals)

**Chosen scope (<2h)**: Ship a frontend-safe `airship discover` orchestrator that produces a static, cacheable status page and a CDN-only manifest so the frontend can render health/state without SSR/backend calls.

**Why this wins**:
- Immediate UX win: status/health visible without backend roundtrips.
- Removes runtime backend dependency for status rendering (reduces surface area + improves cacheability).
- Fits <2h scope: one CLI script + one static asset + one small frontend component.
- Aligns with “frontend-safe” and “CDN-only” patterns (avoid runtime API/auth, prefer CDN fetches).

---

## Implementation plan (concrete, actionable)

1. Create `scripts/airship-discover.js` (Node)
   - Shebang `#!/usr/bin/env node`
   - `set -euo pipefail` equivalent via strict JS + process.exit(1) on critical failures.
   - Discover targets:
     - Prefer local files: `services/*/health.json` (if present) for deterministic, offline-friendly checks.
     - Fallback to HTTP probes: known endpoints (`http://localhost:3000`, `http://localhost:8000/health`, `http://localhost:8001/health`) with timeout and retry.
   - Normalize to schema:
     - `{ slug, name, status, version?, lastCheck, endpoints: [{ url, status, code, latencyMs }], error? }`
   - Write:
     - `dist/manifest.json` (machine-readable, CDN path)
     - `dist/status.html` (simple HTML shell with preloaded manifest + noscript fallback)
   - Exit non-zero on critical failures (so CI can fail).

2. Add frontend component: `src/components/AirshipStatus/AirshipStatus.jsx`
   - Fetch `/dist/manifest.json` (CDN path, no auth, `cache: "no-store"` for freshness).
   - Render badges/cards per service with clear status colors.
   - Optional polling (30s) with `setInterval`; cleanup on unmount.
   - Graceful fallback if manifest missing or fetch fails (cached last-known or inline placeholder).

3. Wire into build/dev
   - Add npm scripts:
     - `"discover": "node scripts/airship-discover.js"`
     - `"build:status": "npm run discover"`
     - Add `npm run discover` to `prestart` or document as manual step in README.
   - Ensure `dist/` (or `public/`) is served statically by dev server/CDN.

4. Verification
   - Run `npm run discover` → confirm `dist/manifest.json` and `dist/status.html` produced.
   - Start dev server → visit page → confirm component renders without backend calls.
   - Test offline/failure cases (remove manifest, block network) → confirm graceful fallback.

---

## Code snippets

### scripts/airship-discover.js
```js
#!/usr/bin/env node
"use strict";

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { promisify } from "util";
import child_process from "child_process";

const exec = promisify(child_process.exec);
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const OUT_DIR = path.resolve(__dirname, "..", "dist");
const MANIFEST_PATH = path.join(OUT_DIR, "manifest.json");
const HTML_PATH = path.join(OUT_DIR, "status.html");

const TIMESTAMP = new Date().toISOString();
const HASH = require("crypto")
  .createHash("sha256")
  .update(TIMESTAMP)
  .digest("hex")
  .slice(0, 12);

const KNOWN_ENDPOINTS = [
  { slug: "arkship-ui", name: "Arkship UI", url: "http://localhost:3000", type: "http" },
  { slug: "arkship-api", name: "Arkship API", url: "http://localhost:8000/health", type: "http" },
  { slug: "surrogate", name: "Surrogate", url: "http://localhost:8001/health", type: "http" },
];

function tryReadLocalHealth(slug) {
  const candidates = [
    path.join(__dirname, "..", "services", slug, "health.json"),
    path.join(__dirname, "..", "..", "services", slug, "health.json"),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) {
      try {
        return JSON.parse(fs.readFileSync(p, "utf8"));
      } catch {
        return null;
      }
    }
  }
  return null;
}

async function probeHttp(url, timeoutMs = 5000) {
  const start = Date.now();
  try {
    // Use curl for consistent timeout behavior across environments
    const { stdout, stderr } = await exec(
      `curl -fs --max-time ${Math.ceil(timeoutMs / 1000)} -o /dev/null -w "%{http_code}" "${url.replace(/"/g, '\\"')}"`,
      { timeout: timeoutMs + 1000 }
    );
    const code = Number(stdout.trim());
    const latencyMs = Date.now() - start;
    return {
      status: code >= 200 && code < 400 ? "healthy" : "unhealthy",
      code,
      latencyMs,
      error: null,
    };
  } catch (err) {
    const latencyMs = Date.now() - start;
    return {
      status: "unhealthy",
      code: 503,
      latencyMs,
      error: err.message || String(err),
    };
  }
}

async function discoverServices() {
  const results = [];

  for (const ep of KNOWN_ENDPOINTS) {
    const local = tryReadLocalHealth(ep.slug);
    let endpoints = [];
    let status = "unknown";
    let version = local?.version || null;

    if (local) {
      // Use local health file as primary source
      endpoints.push({
        url: `file:services/${ep.slug}/health.json`,
        status: local.status || "healthy",
        code: local.code || 200,
        latencyMs: 0,
        error: null,
      });
      status = local.status || "healthy";
    } else {
      // Fallback to HTTP probe
      const probe = await probeHttp(ep.url);
      endpoints.push({
        url: ep.url,
        ...probe,
      });
      status = probe.status;
    }

    results.push({
      slug: ep.slug,
      name: ep.name,
      status,
      version,
      lastCheck: TIMESTAMP,
      endpoints,
    });
  }

  return results;
}

async function main() {
  try {
    const services = await discoverServices();
    const manifest = {
      generated_at: TIMESTAMP,
      hash: HASH,
      services,
    };

    fs.mkdirSync(OUT_DIR, { recursive: true });
    fs.writeFileSync(MANIFEST_PATH, JSON.stringify(manifest, null, 2), "utf8");

    // Simple HTML shell with preloaded manifest and noscript fallback
    const rowsHtml = manifest.services
      .map(
        (s) => `
      <div class="card ${s.status}">
        <strong>${escapeHtml(s.name)}</strong>
        <br/>
        <span class="meta">${escapeHtml(s.status)} — ${s.endpoints?.[0]?.latencyMs ?? "?"}ms</span>
        ${s.version ? `<br/><small>v${escapeHtml(s.version)}</small>` : ""}
      </div>`
      )
      .join("\n");

    fs.writeFileSync(
      HTML_PATH,
      `<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Arkship Status</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <link rel="manifest" href="/dist/manifest.json">
  <style>
    body { font-family: system-ui, sans-serif; padding: 2rem; }
    .card { border: 1px solid #ddd; padding: 1rem; margin-bottom: 0.5rem;
