# Costinel / discovery

## Final Synthesized Implementation

**Highest-Value Incremental Improvement:**  
Deploy a **Top-Hub Signal Panel** on the Costinel dashboard that surfaces the most-connected hub (default **MOC**) and its top 3 actionable cost-impact proposals using a **CDN-first, zero-API-during-runtime** pattern. This delivers immediate user value, avoids Hugging Face rate limits, and aligns with *Sense + Signal — ไม่ Execute*.

---

### Correctness Decisions & Contradictions Resolved
1. **Runtime vs. Build-time fetching:**  
   Use **runtime CDN fetch** (Candidate 1) for always-current data without rebuilds, but **cache aggressively** (`stale-while-revalidate`) to avoid latency. Candidate 2’s build-time-only approach risks stale signals.
2. **Data source:**  
   Commit a lightweight `hubs-index.json` (Candidate 2) for hub metadata, but fetch per-hub proposal JSON (e.g., `moc-proposals.json`) from CDN at runtime (Candidate 1). This balances fast hub resolution with fresh signal content.
3. **Component type:**  
   Implement as a **client component** (Candidate 1) to enable dynamic CDN fetches and optimistic UI; Candidate 2’s server-only approach would require API routes or build-time generation, adding complexity.
4. **Actionability:**  
   Require `impact_usd_month`, `confidence`, and `cdn_source` on every proposal (Candidate 1) to ensure signals are measurable and traceable. Candidate 2’s “short ratio” was too vague.

---

### Implementation Plan (≤2h)

#### 1. Create CDN Artifacts (25m)
- **`public/data/hubs-index.json`** (committed):  
  ```json
  {
    "top_hub": "MOC",
    "generated_at": "2026-05-03T02:37:25Z",
    "hubs": [
      { "slug": "moc", "name": "MOC", "rank": 1, "file": "moc-proposals.json" }
    ]
  }
  ```
- **`public/data/top-hub/moc-proposals.json`** (committed):  
  ```json
  {
    "hub": "MOC",
    "generated_at": "2026-05-03T02:37:25Z",
    "proposals": [
      {
        "id": "moc-ri-001",
        "title": "Reduce RIs in us-east-1",
        "impact_usd_month": 42000,
        "confidence": 0.92,
        "tags": ["RI", "Coverage", "AWS"],
        "cdn_source": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/MOC/moc-ri-001.md"
      },
      {
        "id": "moc-snap-002",
        "title": "Snapshots >30d unattached",
        "impact_usd_month": 18500,
        "confidence": 0.87,
        "tags": ["Storage", "Cleanup", "AWS"],
        "cdn_source": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/MOC/moc-snap-002.md"
      },
      {
        "id": "moc-ip-003",
        "title": "Idle Elastic IPs",
        "impact_usd_month": 7200,
        "confidence": 0.81,
        "tags": ["Network", "Cleanup", "AWS"],
        "cdn_source": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/MOC/moc-ip-003.md"
      }
    ]
  }
  ```

#### 2. Add Hub Resolver (15m)
`src/lib/hubs.ts`
```ts
import type { TopHubData } from '@/types';

export async function getTopHub(): Promise<{ slug: string; name: string }> {
  // In production, fetch from CDN with SWR; here we default to MOC.
  return { slug: 'moc', name: 'MOC' };
}

export async function getHubSignals(slug: string): Promise<TopHubData | null> {
  try {
    const res = await fetch(`/data/top-hub/${slug}-proposals.json`, {
      next: { revalidate: 3600 }, // ISR: 1h cache
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}
```

#### 3. TopHubSignalPanel Component (45m)
`components/TopHubSignalPanel.tsx`
```tsx
'use client';

import { useEffect, useState, use } from 'react';
import { ExternalLink } from 'lucide-react';

interface Proposal {
  id: string;
  title: string;
  impact_usd_month: number;
  confidence: number;
  tags: string[];
  cdn_source: string;
}

interface TopHubData {
  hub: string;
  generated_at: string;
  proposals: Proposal[];
}

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // CDN-first, zero-auth fetch
    fetch('/data/top-hub/moc-proposals.json', { cache: 'no-store' })
      .then((r) => r.json())
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch((err) => {
        console.error('Failed to load top-hub proposals', err);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <p className="text-sm text-muted-foreground">Loading top-hub signals…</p>
      </div>
    );
  }

  if (!data || !data.proposals.length) {
    return null;
  }

  const totalImpact = data.proposals.reduce((s, p) => s + p.impact_usd_month, 0);

  return (
    <div className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h3 className="font-semibold">Top-Hub Signals</h3>
          <p className="text-xs text-muted-foreground">
            Most-connected hub: <span className="font-mono font-medium">{data.hub}</span>
          </p>
        </div>
        <span className="text-xs text-muted-foreground">
          Updated {new Date(data.generated_at).toLocaleDateString()}
        </span>
      </div>

      <div className="mb-3 flex items-baseline gap-2">
        <span className="text-2xl font-bold text-primary">
          ${totalImpact.toLocaleString()}
        </span>
        <span className="text-sm text-muted-foreground">/mo potential</span>
      </div>

      <ul className="space-y-2" role="list">
        {data.proposals.map((p) => (
          <li key={p.id} className="flex gap-3 rounded-md border p-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-start justify-between gap-2">
                <p className="text-sm font-medium leading-tight">{p.title}</p>
                <a
                  href={p.cdn_source}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="shrink-0 pt-0.5 text-muted-foreground hover:text-foreground"
                  title="Open source"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>
              </div>
              <div className="mt-1 flex items-center gap-2">
                <span className="text-xs font-medium text-emerald-600 dark:text-emerald-400">
                  ${p.impact_usd
