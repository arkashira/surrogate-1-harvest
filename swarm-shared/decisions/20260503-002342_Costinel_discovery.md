# Costinel / discovery

## Final Implementation Plan — Costinel “Top-Hub Signal” Card

**Core value**: a read-only dashboard card that surfaces the most-connected hub from the knowledge-rag graph (e.g., “MOC”) with key contextual insights. Reuses existing graph, follows Costinel “Sense + Signal” patterns, and ships in ≤2 hours with zero backend writes.

---

### Chosen stack & why
- **Backend**: expose (or reuse) a read-only GET `/api/knowledge-rag/top-hub`.  
  - If an endpoint already exists, use it.  
  - If not, add a minimal FastAPI route (or framework equivalent) that wraps the existing `knowledge_rag.graph` query.
- **Frontend**: React/TypeScript card using existing design tokens and dashboard grid.  
  - Client fetcher in `/src/lib/knowledge-rag.ts`.  
  - Component in `components/dashboard/TopHubSignalCard.tsx`.  
- **Data contract** (canonical, resolves contradictions):
  ```ts
  interface HubInsight {
    hub: string;
    degree: number;   // connection count (rename from "connections" for clarity)
    summary: string;
    lastUpdated: string; // ISO timestamp
    relatedDocs: Array<{ title: string; url: string }>;
  }
  ```

---

### Concrete steps (ordered for ≤2h delivery)

1. **Locate or create the graph query**  
   - Expected: existing module `knowledge_rag.graph.get_top_hub()` or similar.  
   - If absent, implement a minimal function returning `(hub, degree, summary, relatedDocs)`.

2. **Expose read-only endpoint**  
   - Add `/api/knowledge-rag/top-hub` (GET) returning `HubInsight`.  
   - No writes; cacheable but use `no-store` during dev to avoid staleness.

3. **Add client fetcher**  
   - Create `/src/lib/knowledge-rag.ts` with `fetchTopHub(): Promise<HubInsight | null>`.  
   - Handle network errors gracefully; log locally.

4. **Build card component**  
   - Create `components/dashboard/TopHubSignalCard.tsx`.  
   - States: loading → data → error/unavailable.  
   - Display: hub name, degree, summary, related docs list.  
   - Use existing design tokens and card styles.

5. **Insert into ops dashboard**  
   - Add card to `OpsDashboard` in the “Signals” section or first grid slot.

6. **Test & deploy**  
   - Verify locally with mock and real endpoint.  
   - Deploy via existing CI/CD.

---

### Canonical code (minimal, production-ready)

**1) Backend endpoint (FastAPI example)**
```python
# api/knowledge_rag.py
from fastapi import APIRouter
from knowledge_rag.graph import get_top_hub  # existing or new

router = APIRouter()

@router.get("/knowledge-rag/top-hub")
def top_hub() -> dict:
    hub, degree, summary, related = get_top_hub()
    return {
        "hub": hub,
        "degree": degree,
        "summary": summary,
        "lastUpdated": datetime.utcnow().isoformat() + "Z",
        "relatedDocs": [{"title": d.title, "url": d.url} for d in related],
    }
```

**2) Client fetcher**
```ts
// src/lib/knowledge-rag.ts
export interface HubInsight {
  hub: string;
  degree: number;
  summary: string;
  lastUpdated: string;
  relatedDocs: Array<{ title: string; url: string }>;
}

export async function fetchTopHub(): Promise<HubInsight | null> {
  try {
    const res = await fetch("/api/knowledge-rag/top-hub", {
      method: "GET",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });

    if (!res.ok) throw new Error(`Failed: ${res.status}`);
    return res.json();
  } catch (err) {
    console.error("[knowledge-rag] fetchTopHub error:", err);
    return null;
  }
}
```

**3) Card component**
```tsx
// src/components/dashboard/TopHubSignalCard.tsx
import { useEffect, useState } from "react";
import { fetchTopHub, type HubInsight } from "@/lib/knowledge-rag";

export default function TopHubSignalCard() {
  const [insight, setInsight] = useState<HubInsight | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchTopHub().then((data) => {
      setInsight(data);
      setLoading(false);
    });
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border bg-card p-6">
        <p className="text-sm text-muted-foreground">Loading top hub signal…</p>
      </div>
    );
  }

  if (!insight) {
    return (
      <div className="rounded-lg border bg-card p-6">
        <p className="text-sm text-muted-foreground">Top hub signal unavailable.</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card p-6 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Top-Hub Signal
          </p>
          <h3 className="mt-1 text-2xl font-bold">{insight.hub}</h3>
          <p className="mt-2 text-sm text-muted-foreground">
            {insight.degree.toLocaleString()} connections
          </p>
        </div>
      </div>

      <p className="mt-3 text-sm text-foreground">{insight.summary}</p>

      {insight.relatedDocs.length > 0 && (
        <ul className="mt-4 space-y-1">
          {insight.relatedDocs.map((doc, i) => (
            <li key={i}>
              <a
                href={doc.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-blue-600 hover:underline"
              >
                {doc.title}
              </a>
            </li>
          ))}
        </ul>
      )}

      <p className="mt-4 text-xs text-muted-foreground">
        Updated {new Date(insight.lastUpdated).toLocaleString()}
      </p>
    </div>
  );
}
```

**4) Add to dashboard**
```tsx
// pages/ops-dashboard.tsx or components/dashboard/OpsDashboard.tsx
<Grid>
  {/* existing cards ... */}
  <Grid.Item span={6}>
    <TopHubSignalCard />
  </Grid.Item>
</Grid>
```

---

### Acceptance criteria
- Card appears on the ops dashboard and shows the current top hub from knowledge-rag.
- Read-only; no writes or mutations.
- Fails gracefully (loading → unavailable) if endpoint is unreachable.
- Uses existing design tokens and dashboard layout.
- Deployable via existing CI/CD.

**Estimated effort**: 1–1.5 hours (endpoint + fetcher + component + integration).
