# Costinel / quality

## Highest-Value Incremental Improvement (<2h)

**Add a Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals from the knowledge graph — **CDN-first, rate-limit-safe, zero-API-during-render**.

### Why this ships fast
- Reuses existing knowledge-rag/graph assets (MOC already surfaced in past patterns).
- Pure read-only UI + one-time file-list fetch from Mac orchestrator → CDN-only in app.
- No backend changes, no HF API calls during render, no training pipeline involvement.
- Fits the “Sense + Signal — ไม่ Execute” philosophy.

---

## Implementation Plan (≤2h)

| Step | Owner | Time | Details |
|------|-------|------|---------|
| 1. Fetch & embed file list (once) | Mac orchestrator | 10m | `list_repo_tree(path="knowledge-rag/hubs", recursive=False)` for today’s folder → save `hub-files.json`. Commit to repo or place in `public/data/`. |
| 2. Build CDN fetch utility | FE (React) | 20m | `fetchHubJson(path)` using `https://huggingface.co/datasets/{repo}/resolve/main/{path}`. No auth header. |
| 3. Determine top hub | FE | 10m | Default = `MOC`. If `hub-files.json` exists, pick hub with highest `degree` or last updated. |
| 4. Top-Hub Signal Panel component | FE | 45m | Card showing: hub name, connection count, top 3 proposals (title, impact, owner, due). Skeleton loader + error boundary. |
| 5. Integrate into dashboard | FE | 15m | Place below “Cost Forecast” or in right rail. Responsive (desktop/mobile). |
| 6. Polish & test | FE | 20m | CDN failure fallback to local static JSON. Accessibility (aria labels). Dark mode. |

---

## Code Snippets

### 1) Mac orchestrator — generate file list (run once after rate-limit window)
```bash
#!/usr/bin/env bash
# scripts/generate-hub-file-list.sh
set -euo pipefail

REPO="AXENTX/Costinel"
OUT="public/data/hub-files.json"

# Requires HF_TOKEN in env for list_repo_tree (API call)
python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi
api = HfApi(token=os.getenv("HF_TOKEN"))
tree = api.list_repo_tree(repo_id="$REPO", path="knowledge-rag/hubs", recursive=False)
files = [{"path": f.path, "size": f.size} for f in tree if f.type == "file"]
os.makedirs(os.path.dirname("$OUT"), exist_ok=True)
with open("$OUT", "w") as f:
    json.dump(files, f, indent=2)
print(f"Wrote {len(files)} files to $OUT")
PY
```

Make executable and run:
```bash
chmod +x scripts/generate-hub-file-list.sh
bash scripts/generate-hub-file-list.sh
```

---

### 2) CDN fetch utility (React)
```ts
// lib/cdn.ts
export const CDN_ROOT = "https://huggingface.co/datasets/AXENTX/Costinel/resolve/main";

export async function fetchHubJson<T = unknown>(path: string): Promise<T> {
  const url = `${CDN_ROOT}/${path}`;
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status} ${url}`);
  return res.json() as Promise<T>;
}
```

---

### 3) Top-Hub Signal Panel component
```tsx
// components/TopHubSignalPanel.tsx
"use client";
import { useEffect, useState } from "react";
import { fetchHubJson } from "@/lib/cdn";

interface Proposal {
  title: string;
  impact: "high" | "medium" | "low";
  owner?: string;
  due?: string;
  description: string;
}

interface HubData {
  name: string;
  degree: number;
  proposals: Proposal[];
}

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    async function load() {
      try {
        // Default to MOC; can be overridden by hub-files.json if desired
        const data = await fetchHubJson<HubData>("knowledge-rag/hubs/MOC.json");
        if (mounted) {
          setHub(data);
          setLoading(false);
        }
      } catch (err) {
        if (mounted) {
          setError(err instanceof Error ? err.message : "Unknown error");
          setLoading(false);
        }
      }
    }
    load();
    return () => { mounted = false; };
  }, []);

  if (loading) {
    return (
      <div className="rounded-xl border bg-card p-6">
        <div className="h-6 w-32 bg-muted rounded animate-pulse mb-4" />
        <div className="space-y-3">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-16 bg-muted rounded animate-pulse" />
          ))}
        </div>
      </div>
    );
  }

  if (error || !hub) {
    return (
      <div className="rounded-xl border bg-card p-6 text-sm text-muted-foreground">
        Signals unavailable — using local governance rules.
      </div>
    );
  }

  const impactColor = {
    high: "text-red-600 bg-red-50 dark:text-red-400 dark:bg-red-500/10",
    medium: "text-amber-600 bg-amber-50 dark:text-amber-400 dark:bg-amber-500/10",
    low: "text-emerald-600 bg-emerald-50 dark:text-emerald-400 dark:bg-emerald-500/10",
  };

  return (
    <div className="rounded-xl border bg-card p-6">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="font-semibold text-lg">Top-Hub Signals</h3>
          <p className="text-sm text-muted-foreground">
            {hub.name} — {hub.degree} connections
          </p>
        </div>
        <span className="text-xs px-2 py-1 rounded bg-primary/10 text-primary">
          Sense + Signal
        </span>
      </div>

      <div className="space-y-3">
        {hub.proposals.slice(0, 3).map((p, i) => (
          <div
            key={i}
            className="p-3 rounded-lg border bg-background/50"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="flex-1 min-w-0">
                <p className="font-medium text-sm truncate">{p.title}</p>
                <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
                  {p.description}
                </p>
              </div>
              <span
                className={`shrink-0 text-xs px-2 py-1 rounded whitespace-nowrap ${
                  impactColor[p.impact]
                }`}
              >
                {p.impact}
              </span>
            </div>
            {(p.owner || p.due) && (
              <p className="text-xs text-muted-foreground mt-2">
                {p.owner && <>Owner: {p.owner}</>}
                {p.owner && p.due && " · "}
                {p.due && <>Due: {p.due}</>}
              </p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
```

---

### 4) Add to dashboard page
```tsx
// app/dashboard/page.tsx
