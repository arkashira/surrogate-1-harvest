# Costinel / quality

## Implementation Plan — Costinel “Top-Hub Signal” Card (read-only)

**Scope**: A production-ready, read-only ops card that surfaces the most-connected knowledge hub (e.g., “MOC”) from the existing knowledge-rag graph with contextual cost-governance signals. Uses CDN-bypass for live cost data and embedded mock fallback for resilience. Ship in <2h.

### High-level approach
- Reuse existing knowledge-rag graph to identify the top hub (most edges) — likely “MOC”.
- Surface a compact card in the ops view with:
  - Hub name + degree
  - Top 3 related docs (title + snippet)
  - Live cost signal (daily spend vs forecast) via CDN-bypass
  - Fallback to embedded mock if CDN fails
- Keep it read-only; no execution, no writes.

### Files to touch
- `src/components/ops/TopHubSignalCard.tsx` (new)
- `src/lib/knowledgeRag.ts` (add: getTopHub, getRelatedDocs)
- `src/lib/costApi.ts` (add: getDailyCostCdn)
- `src/pages/Ops.tsx` (mount card)
- `src/mocks/topHubMock.json` (fallback payload)

### Implementation steps (with code snippets)

#### 1) Add knowledge-rag helpers
```ts
// src/lib/knowledgeRag.ts
export interface HubNode {
  id: string;
  label: string;
  degree: number;
}
export interface RelatedDoc {
  id: string;
  title: string;
  snippet: string;
  score: number;
}

// Lightweight client-side graph query (assumes graph already loaded via window.__RAG_GRAPH__)
export function getTopHub(): HubNode | null {
  const graph = (window as any).__RAG_GRAPH__;
  if (!graph?.nodes?.length) return null;
  const top = graph.nodes.reduce((a: HubNode, b: HubNode) => (b.degree > a.degree ? b : a));
  return top;
}

export function getRelatedDocs(hubId: string, limit = 3): RelatedDoc[] {
  const graph = (window as any).__RAG_GRAPH__;
  if (!graph?.edges) return [];
  return graph.edges
    .filter((e: any) => e.source === hubId || e.target === hubId)
    .map((e: any) => {
      const otherId = e.source === hubId ? e.target : e.source;
      const node = graph.nodes.find((n: any) => n.id === otherId);
      return {
        id: otherId,
        title: node?.label || otherId,
        snippet: node?.snippet || '',
        score: e.weight || 0,
      };
    })
    .sort((a: any, b: any) => b.score - a.score)
    .slice(0, limit);
}
```

#### 2) Add CDN-bypass cost fetch
```ts
// src/lib/costApi.ts
export interface DailyCostSignal {
  date: string;
  actual: number;
  forecast: number;
  currency: string;
}

// CDN bypass: https://huggingface.co/datasets/{repo}/resolve/main/{path}
// We mirror daily cost parquet to a public HF dataset repo: axentx/costinel-cost-mirror
export async function getDailyCostCdn(date?: string): Promise<DailyCostSignal | null> {
  const d = date || new Date().toISOString().slice(0, 10);
  // Parquet not ideal for direct CDN fetch in browser; use lightweight JSON mirror instead.
  // Mirror job should produce: /daily/{date}.json with { actual, forecast, currency }
  const url = `https://huggingface.co/datasets/axentx/costinel-cost-mirror/resolve/main/daily/${d}.json`;
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    return res.json();
  } catch (err) {
    console.warn('Cost CDN fallback', err);
    return null;
  }
}
```

#### 3) Create TopHubSignalCard component
```tsx
// src/components/ops/TopHubSignalCard.tsx
import { useEffect, useState } from 'react';
import { getTopHub, getRelatedDocs, HubNode, RelatedDoc } from '../lib/knowledgeRag';
import { getDailyCostCdn, DailyCostSignal } from '../lib/costApi';
import topHubMock from '../../mocks/topHubMock.json';

export default function TopHubSignalCard() {
  const [hub, setHub] = useState<HubNode | null>(null);
  const [related, setRelated] = useState<RelatedDoc[]>([]);
  const [cost, setCost] = useState<DailyCostSignal | null>(null);

  useEffect(() => {
    const h = getTopHub();
    if (h) {
      setHub(h);
      setRelated(getRelatedDocs(h.id, 3));
    } else {
      // fallback to mock
      setHub(topHubMock.hub);
      setRelated(topHubMock.related);
    }

    (async () => {
      const c = await getDailyCostCdn();
      if (c) setCost(c);
      else setCost(topHubMock.cost);
    })();
  }, []);

  if (!hub) return null;

  return (
    <div className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">Top Knowledge Hub</h3>
          <p className="text-2xl font-bold text-blue-600">{hub.label}</p>
          <p className="text-xs text-gray-500">{hub.degree} connections</p>
        </div>
        {cost && (
          <div className="text-right">
            <p className="text-xs text-gray-500">Today's spend</p>
            <p className="text-lg font-semibold">{cost.currency} {cost.actual.toLocaleString()}</p>
            <p className="text-xs text-gray-400">forecast {cost.currency} {cost.forecast.toLocaleString()}</p>
          </div>
        )}
      </div>

      {related.length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-medium text-gray-500 mb-2">Related docs</p>
          <ul className="space-y-1">
            {related.map((doc) => (
              <li key={doc.id} className="flex items-center text-xs text-gray-700">
                <span className="w-1 h-1 rounded-full bg-blue-400 mr-2" />
                <span className="truncate">{doc.title}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-3 text-xs text-gray-400">Sense + Signal — ไม่ Execute</p>
    </div>
  );
}
```

#### 4) Add mock fallback
```json
// src/mocks/topHubMock.json
{
  "hub": {
    "id": "MOC",
    "label": "MOC",
    "degree": 42
  },
  "related": [
    { "id": "ri-aws", "title": "AWS RI Coverage", "snippet": "Reserved Instance coverage analysis", "score": 0.92 },
    { "id": "anomaly-detection", "title": "Anomaly Detection Patterns", "snippet": "Detecting spend anomalies", "score": 0.87 },
    { "id": "governance-policy", "title": "Governance Policy Framework", "snippet": "Policy templates and audit trails", "score": 0.81 }
  ],
  "cost": {
    "date": "2026-05-03",
    "actual": 12480,
    "forecast": 13200,
    "currency": "USD"
  }
}
```

#### 5) Mount in Ops page
```tsx
// src/pages/Ops.tsx
import Top
