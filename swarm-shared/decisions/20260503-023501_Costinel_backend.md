# Costinel / backend

## Final Synthesized Plan — Highest-Value Incremental Improvement (<2h)

**Goal:** Add a “Top-Hub Signal Panel” to the Costinel dashboard that surfaces the most-connected hub (default **MOC**) and its top 3 actionable, cost-impact proposals — **CDN-first, rate-limit-safe, zero API calls during render**.

---

### Why this is the highest-value choice
- **Applies proven patterns**: #knowledge-rag #graph #hub + CDN bypass.
- **Zero runtime risk**: no HuggingFace API calls during render; no backend compute cost or rate-limit exposure.
- **Read-only, safe UX**: demonstrates “Sense + Signal — doesn’t Execute”.
- **Fast to ship**: ~2h with clear, minimal scope.
- **High visible impact**: immediately shows value on the dashboard.

---

### Resolved design choices (correctness + actionability)

| Decision | Resolution |
|---|---|
| **Runtime endpoint vs static file** | Prefer **static file + CDN** (Candidate 2) for true zero-runtime-risk. Add a **lightweight backend redirect/fallback** (Candidate 1) only if you need runtime flexibility. Default to static-first. |
| **Where to generate** | Generate at **build/CI time** (script) and commit or stage `public/data/top-hub-signals.json`. Avoid runtime HF API calls entirely. |
| **Data shape** | Use Candidate 1’s rich schema (includes `estimated_savings_usd`, `action_url`, `tags`, `impact`) — it’s more actionable and consistent with Costinel. |
| **Caching** | Long `Cache-Control` + immutable filename (or content-hash) for CDN safety. |
| **Frontend** | Candidate 1’s React component is production-ready; keep it with small style fixes and accessibility improvements. |

---

### Implementation Plan (≤2h)

#### 1) Build-time generator script (30–45m)
File: `scripts/build-top-hub-signals.js` (Node)

```js
// scripts/build-top-hub-signals.js
// Run in CI or local dev (mac) after knowledge-rag step.
// Uses one list_repo_tree or local mirror folder to find latest mirror-merged/{date}/top-hub-MOC.json
// Projects to public/data/top-hub-signals.json

const fs = require("fs");
const path = require("path");

function findLatestMirrorFolder(base) {
  const items = fs.readdirSync(base, { withFileTypes: true })
    .filter((d) => d.isDirectory() && d.name.startsWith("mirror-merged"))
    .map((d) => d.name)
    .sort()
    .reverse();
  return items[0] || null;
}

function build() {
  const mirrorBase = path.join(process.cwd(), "batches", "mirror-merged");
  const latest = findLatestMirrorFolder(mirrorBase);
  if (!latest) {
    console.warn("No mirror-merged folder found. Using fallback.");
    writeFallback();
    return;
  }

  const topHubPath = path.join(mirrorBase, latest, "top-hub-MOC.json");
  let payload;
  if (fs.existsSync(topHubPath)) {
    const raw = JSON.parse(fs.readFileSync(topHubPath, "utf8"));
    payload = transform(raw);
  } else {
    payload = fallbackPayload();
  }

  const outDir = path.join(process.cwd(), "public", "data");
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(
    path.join(outDir, "top-hub-signals.json"),
    JSON.stringify(payload, null, 2)
  );
  console.log("Wrote public/data/top-hub-signals.json");
}

function transform(raw) {
  // Adapt raw graph output to Costinel signal shape.
  // Keep this aligned with data contract below.
  const signals = (raw.signals || []).slice(0, 3).map((s) => ({
    id: s.id || `signal-${Math.random().toString(36).slice(2, 9)}`,
    title: s.title || "Untitled signal",
    impact: s.impact || "medium",
    estimated_savings_usd: Number(s.estimated_savings_usd || 0),
    description: s.rationale || s.description || "",
    action_url: s.action_url || `/proposals/${s.id || "details"}`,
    tags: s.tags || []
  }));

  return {
    hub: raw.hub || "MOC",
    updated_at: raw.updated_at || new Date().toISOString(),
    signals
  };
}

function fallbackPayload() {
  return {
    hub: "MOC",
    updated_at: new Date().toISOString(),
    signals: [
      {
        id: "moc-ri-coverage",
        title: "RI Coverage Gap in us-east-1",
        impact: "high",
        estimated_savings_usd: 42000,
        description: "37% of steady-state workloads are on-demand; convertible RIs available with 1yr No Upfront.",
        action_url: "/proposals/ri-coverage-moc",
        tags: ["aws", "ri", "compute"]
      },
      {
        id: "moc-snapshot-retention",
        title: "Orphaned EBS Snapshots",
        impact: "medium",
        estimated_savings_usd: 8500,
        description: "210 snapshots >90d with no linked AMI; lifecycle policy recommended.",
        action_url: "/proposals/snapshot-cleanup-moc",
        tags: ["aws", "storage", "snapshot"]
      },
      {
        id: "moc-idle-nat-gw",
        title: "Idle NAT Gateways",
        impact: "medium",
        estimated_savings_usd: 3600,
        description: "2 NAT GWs in dev accounts with <5GB processed last 14d; candidate for schedule-based stop.",
        action_url: "/proposals/natgw-schedule-moc",
        tags: ["aws", "network", "nat"]
      }
    ]
  };
}

function writeFallback() {
  const outDir = path.join(process.cwd(), "public", "data");
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(
    path.join(outDir, "top-hub-signals.json"),
    JSON.stringify(fallbackPayload(), null, 2)
  );
}

if (require.main === module) {
  build();
}
```

Add to `package.json` scripts:
```json
"scripts": {
  "build:top-hub": "node scripts/build-top-hub-signals.js"
}
```

Run in CI after knowledge-rag step:
```bash
npm run build:top-hub
```

---

#### 2) Data contract (5m)
File: `public/data/top-hub-signals.json` (committed or generated)

```json
{
  "hub": "MOC",
  "updated_at": "2026-05-03T02:30:00Z",
  "signals": [
    {
      "id": "moc-ri-coverage",
      "title": "RI Coverage Gap in us-east-1",
      "impact": "high",
      "estimated_savings_usd": 42000,
      "description": "37% of steady-state workloads are on-demand; convertible RIs available with 1yr No Upfront.",
      "action_url": "/proposals/ri-coverage-moc",
      "tags": ["aws", "ri", "compute"]
    },
    {
      "id": "moc-snapshot-retention",
      "title": "Orphaned EBS Snapshots",
      "impact": "medium",
      "estimated_savings_usd": 8500,
      "description": "210 snapshots >90d with no linked AMI; lifecycle policy recommended.",
      "action_url": "/proposals/snapshot-cleanup-moc",
      "tags": ["aws", "storage", "snapshot"]
    },
    {
      "id": "moc-idle-nat-gw",
      "title": "Idle NAT Gateways",
      "impact": "medium",
      "estimated_savings_usd": 3600,
      "description": "2 NAT GWs in dev accounts with <5GB processed last 14d; candidate for
