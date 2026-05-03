# Costinel / quality

## Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, telemetry-aware)

### What we ship (highest-value incremental)
- A **non-blocking Top-Hub Signal Panel** on the Costinel dashboard.
- Defaults to hub **MOC** (configurable via `HUB_NAME`).
- Shows: hub title, short description, top 3 actionable cost-impact proposals.
- **Zero HuggingFace API calls during runtime** — uses CDN (`/resolve/main/`) + pre-listed file manifest.
- Graceful fallback states: loading → stale data → empty (no blocking UI).
- Telemetry: lightweight `navigator.sendBeacon` pings for timing + outcome (no cookies/PID).

### Architecture (fits existing patterns)
- **Mac/CLI only**: manifest generation script runs locally (HF API + `list_repo_tree`), outputs `hub-manifest.json`.
- **CDN-first runtime**: dashboard fetches `https://huggingface.co/datasets/.../resolve/main/hub-manifest.json` and per-hub payloads; no `/api/` calls.
- **Lightning-aware**: training/ops can reuse this manifest for surrogate-1 pipelines; no schema mixing.
- **Kaggle-ready**: Bearer auth pattern available if private hubs require token (not default).

---

### File changes (concrete)

#### 1) Add hub manifest generator (dev-only, run on Mac/CI)
`scripts/generate-hub-manifest.js`
```js
#!/usr/bin/env node
/**
 * Generate hub manifest for CDN-first loading.
 * Usage: HUB_NAME=MOC node scripts/generate-hub-manifest.js > public/hub-manifest.json
 *
 * Notes:
 * - Uses HF API once (list_repo_tree) to list hub payloads for a date folder.
 * - CDN URLs are returned (resolve/main) so runtime uses zero API calls.
 * - Designed to be run in CI or locally after rate-limit window clears.
 */

import { HfApi } from "@huggingface/hub";
import fs from "fs";
import path from "path";

const HF_REPO = process.env.HF_REPO || "datasets/axentx/knowledge-hubs";
const HUB_NAME = process.env.HUB_NAME || "MOC";
const DATE_FOLDER = process.env.DATE_FOLDER || new Date().toISOString().slice(0, 10); // e.g., 2026-05-03
const OUT_PATH = process.env.OUT_PATH || "public/hub-manifest.json";

async function main() {
  const api = new HfApi();
  const folderPath = `${DATE_FOLDER}/hubs`;

  try {
    // list non-recursive to avoid pagination explosion
    const tree = await api.listRepoTree({
      repo: HF_REPO,
      path: folderPath,
      recursive: false,
    });

    const hubFiles = (tree || [])
      .filter((f) => f.type === "file" && f.path.toLowerCase().includes(HUB_NAME.toLowerCase()))
      .sort((a, b) => a.path.localeCompare(b.path));

    const manifest = {
      generatedAt: new Date().toISOString(),
      repo: HF_REPO,
      dateFolder: DATE_FOLDER,
      hub: HUB_NAME,
      // CDN-first URLs (no Authorization header required)
      hubPayload: hubFiles.length
        ? `https://huggingface.co/datasets/${HF_REPO}/resolve/main/${hubFiles[0].path}`
        : null,
      availableHubs: Array.from(
        new Set(
          (tree || [])
            .filter((f) => f.type === "file" && f.path.includes("/hubs/"))
            .map((f) => path.basename(f.path, path.extname(f.path)))
        )
      ),
      // lightweight file list for training pipelines (CDN-only ingestion)
      fileList: (tree || [])
        .filter((f) => f.type === "file")
        .map((f) => ({
          path: f.path,
          cdn: `https://huggingface.co/datasets/${HF_REPO}/resolve/main/${f.path}`,
          size: f.size,
        })),
    };

    fs.writeFileSync(OUT_PATH, JSON.stringify(manifest, null, 2), "utf8");
    console.log(`Manifest written to ${OUT_PATH}`);
  } catch (err) {
    // If API fails (rate limit), fallback to last-known manifest or empty
    console.warn("HF API error, producing minimal manifest:", err.message);
    const fallback = {
      generatedAt: new Date().toISOString(),
      repo: HF_REPO,
      dateFolder: DATE_FOLDER,
      hub: HUB_NAME,
      hubPayload: null,
      availableHubs: [HUB_NAME],
      fileList: [],
      error: err.message,
    };
    fs.writeFileSync(OUT_PATH, JSON.stringify(fallback, "utf8"));
  }
}

main();
```

Make executable (if saved as `.sh` wrapper) or ensure CI runs with Node. For cron wrappers, apply pattern fixes:
- Shebang `#!/usr/bin/env bash`
- `chmod +x`
- `SHELL=/bin/bash` in crontab

---

#### 2) Add lightweight telemetry helper (no cookies/PID)
`src/lib/telemetry.js`
```js
/**
 * Lightweight, privacy-first telemetry for UI panels.
 * Uses navigator.sendBeacon where available.
 * No cookies, no user identifiers.
 */

export function trackPanelEvent(panel, event, extras = {}) {
  const payload = {
    panel,
    event,
    href: location?.href?.replace(location?.search, "") || "",
    timestamp: Date.now(),
    ...extras,
  };

  try {
    const blob = new Blob([JSON.stringify(payload)], { type: "application/json" });
    if (navigator?.sendBeacon) {
      navigator.sendBeacon("/_telemetry", blob);
    } else {
      // best-effort fire-and-forget
      fetch("/_telemetry", { method: "POST", body: blob, keepalive: true }).catch(() => {});
    }
  } catch {
    // silent fail
  }
}
```

---

#### 3) Add Top-Hub Signal Panel component (framework-agnostic approach)
`src/components/TopHubSignalPanel.js`
```js
import { trackPanelEvent } from "../lib/telemetry.js";

const HUB_NAME = import.meta.env?.PUBLIC_HUB_NAME || "MOC";
const MANIFEST_URL = import.meta.env?.PUBLIC_HUB_MANIFEST_URL || "/hub-manifest.json";
const CDN_BASE = "https://huggingface.co/datasets";

export async function mountTopHubSignalPanel(container) {
  if (!container) return;

  const start = performance.now();
  trackPanelEvent("TopHubSignalPanel", "mount_start", { hub: HUB_NAME });

  // Render skeleton immediately (non-blocking)
  container.innerHTML = `
    <section class="top-hub-panel" aria-busy="true">
      <header class="panel-header">
        <h3>Top Hub Signal</h3>
        <span class="hub-badge">${HUB_NAME}</span>
      </header>
      <div class="hub-body">
        <div class="hub-loading">Loading signals…</div>
      </div>
    </section>
  `;

  try {
    // Fetch CDN-first manifest (no Authorization header)
    const manifestRes = await fetch(MANIFEST_URL, { cache: "no-cache" });
    if (!manifestRes.ok) throw new Error("Manifest unavailable");

    const manifest = await manifestRes.json();
    const hubUrl = manifest?.hubPayload || `${CDN_BASE}/${manifest?.repo || "datasets/axentx/knowledge-hubs"}/resolve/main/${manifest?.dateFolder || ""}/hubs/${HUB_NAME}.json`;

    // Fetch hub payload from CDN
    const hubRes = await fetch(hubUrl, { cache: "no-cache" });
    let hubData = null;
    let proposals = [];

    if (hubRes.ok) {
      hubData = await hubRes.json();
      proposals = (hubData?.proposals || []).slice(0, 3);
    }

    const elapsed = Math.round(performance.now() - start);
    trackPanelEvent("TopHubSignalPanel", "mount_complete", {
      hub: HUB_NAME,
      hasPayload: !!hubData,
      proposalCount: proposals.length,
      elapsedMs: elapsed,

