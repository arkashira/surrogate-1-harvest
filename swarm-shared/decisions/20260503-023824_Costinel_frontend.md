# Costinel / frontend

## Final Synthesis — Highest-Value Incremental Improvement (<2h)

**Goal**: Add a **Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals — **CDN-first, rate-limit-safe, zero-API-during-training**.

**Core value**: Turns the knowledge graph into immediate frontend value without backend churn or runtime HF API calls. Fits <2h and enforces the architecture rule (no model loads on dev machines).

---

## Unified Implementation Plan (resolved + concrete)

1. **Static artifact generation (once per deploy / CI step)**
   - Generate `public/data/top-hub-signals.json` after the knowledge pipeline finishes.  
   - Schema (minimal, stable):
     ```json
     {
       "hub": "MOC",
       "updatedAt": "2025-06-25T14:30:00Z",
       "signals": [
         {
           "title": "CDN-first dataset access",
           "insight": "Bypass HF datasets API for training files; use raw CDN URLs to avoid rate limits and egress costs.",
           "impactScore": 9.2,
           "costDelta": 18.5,
           "href": "/proposals/cdn-first"
         }
       ]
     }
     ```
   - Implementation options (pick one that matches your current flow):
     - **Mac orchestration script** (if run manually or in CI):
       ```bash
       #!/usr/bin/env bash
       # scripts/generate-hub-index.sh
       set -euo pipefail

       REPO="axentx/costinel-knowledge"
       OUT="public/data/top-hub-signals.json"

       mkdir -p "$(dirname "$OUT")"

       python3 - <<PY
       import json, datetime, os
       from huggingface_hub import list_repo_tree

       REPO = "$REPO"
       tree = list_repo_tree(
           repo_id=REPO,
           path="knowledge/hubs",
           repo_type="dataset",
           recursive=True
       )

       entries = []
       for f in tree:
           if f.type == "file" and f.path.endswith((".jsonl", ".json")):
               entries.append({
                   "path": f.path,
                   "url": f"https://huggingface.co/datasets/{REPO}/resolve/main/{f.path}"
               })

       # In production, replace this stub with real scoring from your RAG output.
       # For now, synthesize top-3 placeholders sorted by impact.
       signals = [
           {
               "title": "CDN-first dataset access",
               "insight": "Bypass HF datasets API for training files; use raw CDN URLs to avoid rate limits and egress costs.",
               "impactScore": 9.2,
               "costDelta": 18.5,
               "href": "/proposals/cdn-first"
           },
           {
               "title": "Rate-limit-safe batch scheduling",
               "insight": "Run heavy ingestion during off-peak HF windows; use jittered retries and backoff.",
               "impactScore": 8.7,
               "costDelta": 12.1,
               "href": "/proposals/rate-limit-safe"
           },
           {
               "title": "Zero-API-during-training",
               "insight": "Stage all artifacts locally or via CDN before training starts; no HF API calls in training loop.",
               "impactScore": 9.5,
               "costDelta": 22.3,
               "href": "/proposals/zero-api-training"
           }
       ]
       signals.sort(key=lambda x: x["impactScore"], reverse=True)

       payload = {
           "hub": "MOC",
           "updatedAt": datetime.datetime.utcnow().isoformat() + "Z",
           "signals": signals[:3]
       }

       with open("$OUT", "w") as fp:
           json.dump(payload, fp, indent=2)
       print(f"Wrote {len(signals)} signals to $OUT")
       PY
       ```
     - **Pipeline export** (preferred if you already run `granite-business-research.sh` + `knowledge-rag`): append a final node that writes the same JSON to `public/data/top-hub-signals.json`. Keep schema identical.

2. **Frontend: CDN-first loader + caching**
   - Fetch `/data/top-hub-signals.json` (static, no auth, no HF API at runtime).
   - Memoize in `localStorage` with short TTL (15–30m) to avoid refetch on every dashboard nav.
   - Defensive: graceful empty state on any failure; never block dashboard render.

   ```ts
   // src/lib/hubSignals.ts
   const INDEX_URL = '/data/top-hub-signals.json';
   const CACHE_KEY = 'costinel:topHubSignals';
   const TTL_MS = 15 * 60 * 1000;

   export interface HubSignal {
     title: string;
     insight: string;
     impactScore: number;
     costDelta: number;
     href: string;
   }

   export interface TopHubPayload {
     hub: string;
     updatedAt: string;
     signals: HubSignal[];
   }

   async function loadFromCDN(url: string): Promise<HubSignal[]> {
     const res = await fetch(url, { cache: 'no-store' });
     if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
     const json = (await res.json()) as TopHubPayload;
     return (json.signals || []).sort((a, b) => b.impactScore - a.impactScore).slice(0, 3);
   }

   export async function getTopHubSignals(): Promise<HubSignal[]> {
     try {
       const cached = localStorage.getItem(CACHE_KEY);
       if (cached) {
         const { ts, data } = JSON.parse(cached);
         if (Date.now() - ts < TTL_MS) return data;
       }

       const signals = await loadFromCDN(INDEX_URL);
       localStorage.setItem(CACHE_KEY, JSON.stringify({ ts: Date.now(), data: signals }));
       return signals;
     } catch {
       return [];
     }
   }
   ```

3. **UI: TopHubSignalPanel component**
   - Location: `src/components/dashboard/TopHubSignalPanel.tsx`.
   - Minimal, accessible, non-blocking.

   ```tsx
   // src/components/dashboard/TopHubSignalPanel.tsx
   import { useEffect, useState } from 'react';
   import { getTopHubSignals, type HubSignal } from '@/lib/hubSignals';
   import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
   import { ArrowRight } from 'lucide-react';

   export function TopHubSignalPanel() {
     const [signals, setSignals] = useState<HubSignal[]>([]);
     const [loading, setLoading] = useState(true);

     useEffect(() => {
       setLoading(true);
       getTopHubSignals()
         .then(setSignals)
         .catch(() => setSignals([]))
         .finally(() => setLoading(false));
     }, []);

     if (loading) return <div className="h-48 animate-pulse bg-muted rounded" />;
     if (!signals.length) return null;

     return (
       <Card>
         <CardHeader className="pb-3">
           <CardTitle className="text-base font-semibold">
             Top-Hub Signals (MOC)
           </CardTitle>
         </CardHeader>
         <CardContent className="space-y-3">
           {signals.map((s) => (
             <div key={s.title} className="border rounded p-3">
               <div className="flex items-start justify-between gap-2">
                 <div>
                   <h4 className="font-medium text-sm">{s.title}</h4>
                   <p className="text-xs text-muted-foreground mt-1">{s.insight}</p>
                   <span
                     className={`text-xs font-medium ${
                       s.costDelta >= 0 ? 'text-emerald-600' : 'text-red-600'
                     }`}
                   >
                     {s.costDelta >= 0 ? '+' : ''}
                     {s
