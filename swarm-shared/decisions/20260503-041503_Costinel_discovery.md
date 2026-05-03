# Costinel / discovery

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a lightweight, resilient “Top-Hub Signal” panel to Costinel that surfaces the most‑connected hub (e.g., “MOC”) with **zero runtime HF API calls**, using CDN-first baked data, graceful fallbacks, and Mac-friendly ops hygiene.

---

### 1) High-value scope (what ships in <2h)
- Add a new panel component: `TopHubSignalPanel`
- CDN-first data strategy:
  - Mac orchestration script pre-lists one date folder via HF API (single call) and saves `file-list.json`
  - Training/data job downloads only needed files via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth, no rate-limit)
  - Produces a tiny baked artifact: `top-hub-latest.json` (hub, score, edges, updated_at)
  - Panel fetches this artifact from CDN (or local static fallback)
- Graceful degradation:
  - If CDN fetch fails → use embedded static fallback (last known hub)
  - If no baked artifact → show “insights unavailable” with refresh hint
- Mac-only orchestration; no local training or heavy compute
- No schema changes to existing models; only additive UI + ops scripts

---

### 2) File layout (additions/modifications)
```
/opt/axentx/Costinel/
├── public/
│   └── data/
│       └── top-hub-latest.json      # baked artifact (committed or CI-copied)
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel.tsx    # new panel
│   ├── lib/
│   │   └── api/
│   │       └── topHub.ts            # CDN fetcher + fallback
│   └── pages/
│       └── Dashboard.tsx            # import & mount panel
├── scripts/
│   ├── mac/
│   │   └── bake-top-hub-files.sh   # Mac orchestration (list + CDN fetch)
│   └── ci/
│       └── update-top-hub-artifact.sh # CI helper to copy artifact into repo/public
└── package.json                     # ensure fetch available (no new deps)
```

---

### 3) Implementation steps & code snippets

#### Step A — Baked artifact contract (`public/data/top-hub-latest.json`)
```json
{
  "hub": "MOC",
  "score": 0.94,
  "edges": 1283,
  "updated_at": "2026-05-03T04:14:41Z",
  "source": "knowledge-rag",
  "note": "Most-connected hub from latest graph run"
}
```

#### Step B — API/fetch layer (`src/lib/api/topHub.ts`)
```ts
// src/lib/api/topHub.ts
const FALLBACK_HUB = {
  hub: "MOC",
  score: 0.91,
  edges: 1240,
  updated_at: "2026-04-27T00:00:00Z",
  source: "fallback",
  note: "Embedded fallback — refresh when CDN available"
};

export type TopHub = typeof FALLBACK_HUB;

export async function fetchTopHub(preferCDN = true): Promise<TopHub> {
  if (!preferCDN) return FALLBACK_HUB;

  try {
    // CDN fetch — no Authorization header (bypasses HF API rate limits)
    const res = await fetch("/data/top-hub-latest.json", {
      cache: "no-store",
      credentials: "same-origin"
    });

    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    const json = await res.json();

    // Basic shape validation
    if (!json?.hub || typeof json.score !== "number") {
      console.warn("[TopHub] Invalid CDN payload, using fallback");
      return FALLBACK_HUB;
    }
    return json as TopHub;
  } catch (err) {
    console.warn("[TopHub] CDN unavailable, using fallback", err);
    return FALLBACK_HUB;
  }
}
```

#### Step C — Panel component (`src/components/TopHubSignalPanel.tsx`)
```tsx
// src/components/TopHubSignalPanel.tsx
"use client";

import { useEffect, useState } from "react";
import { fetchTopHub, type TopHub } from "@/lib/api/topHub";

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState<TopHub | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchTopHub(true).then((h) => {
      setHub(h);
      setLoading(false);
    });
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <p className="text-sm text-muted-foreground">Loading top-hub insights…</p>
      </div>
    );
  }

  if (!hub) return null;

  const isFallback = hub.source === "fallback";

  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-sm font-semibold text-foreground">Top-Hub Signal</h3>
          <p className="text-2xl font-bold tracking-tight">{hub.hub}</p>
          <p className="text-xs text-muted-foreground">
            Score: {(hub.score * 100).toFixed(0)}% &nbsp;|&nbsp; Edges: {hub.edges.toLocaleString()}
          </p>
        </div>
        <div className="text-right">
          <span
            className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
              isFallback
                ? "bg-yellow-100 text-yellow-800"
                : "bg-green-100 text-green-800"
            }`}
          >
            {isFallback ? "Fallback" : "Live"}
          </span>
          <p className="mt-1 text-xs text-muted-foreground">
            Updated: {new Date(hub.updated_at).toLocaleDateString()}
          </p>
        </div>
      </div>

      <p className="mt-3 text-xs text-muted-foreground">
        Most‑connected hub from knowledge‑RAG graph. Sense + Signal — ไม่ Execute.
      </p>

      {isFallback && (
        <p className="mt-2 text-xs text-amber-600">
          CDN artifact unavailable. Run bake script or check CI to refresh.
        </p>
      )}
    </div>
  );
}
```

#### Step D — Mount on Dashboard (`src/pages/Dashboard.tsx`)
```tsx
// Inside your Dashboard layout / grid — example snippet
import TopHubSignalPanel from "@/components/TopHubSignalPanel";

// ...
<div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
  <TopHubSignalPanel />
  {/* other panels */}
</div>
```

#### Step E — Mac orchestration script (`scripts/mac/bake-top-hub-files.sh`)
```bash
#!/usr/bin/env bash
# scripts/mac/bake-top-hub-files.sh
# Mac-only orchestration: list once, then CDN fetch (zero HF API during training)
# Usage: bash scripts/mac/bake-top-hub-files.sh <date-folder> <output-dir>

set -euo pipefail
export SHELL=/bin/bash

HF_REPO="${HF_REPO:-datasets/axentx/knowledge-rag}"
DATE_FOLDER="${1:-$(date +%Y-%m-%d)}"
OUTDIR="${2:-./tmp/top-hub}"
mkdir -p "$OUTDIR"

echo "== Baking top-hub artifact for $DATE_FOLDER =="

# 1) Single API call from Mac (after rate-limit window) to list folder
# Uses huggingface_hub CLI or python one-liner — here we use python for portability
python3 - "$HF_REPO" "$DATE_FOLDER" "$OUTDIR" <<
