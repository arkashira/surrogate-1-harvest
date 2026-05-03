# Costinel / quality

## Final Synthesis — Costinel “Top-Hub Signal” Card (Read-Only)

**Decision principle**: Favor correctness + concrete actionability.  
- **Strictly read-only**: no writes, no runtime mutations, no background jobs, no self-execution.  
- **Fast to ship**: ≤2h, frontend-first, minimal infra.  
- **Correctness**: use static, verifiable snapshot for production safety; keep optional lightweight refresh script for ops.  
- **Actionability**: clear file paths, exact code, deploy steps, and rollback.

---

### 1) Architecture (read-only)
- **Primary data source**: static JSON committed to repo or deployed artifact (`public/data/top-hub.json`).  
  - Guarantees immutability, cacheability, and zero-runtime-cost serving.  
- **Optional refresh**: offline script (`scripts/refresh-top-hub-snapshot.sh`) can regenerate the JSON from knowledge-rag (read-only) and commit it.  
- **No backend API route required** for MVP (reduces surface area). If dynamic behavior is later required, add a read-only API route behind feature flag.

---

### 2) Concrete implementation (frontend-only)

#### A) Static payload (committed or deployed)
`public/data/top-hub.json`
```json
{
  "hub": "MOC",
  "connections": 1247,
  "generatedAt": "2026-05-03T00:00:00Z",
  "contexts": [
    {
      "title": "MOC — Multi-Org Cost model",
      "snippet": "Describes cross-org cost allocation and chargeback patterns.",
      "href": "/docs/moc-cost-model"
    },
    {
      "title": "Governance playbook (MOC)",
      "snippet": "Signal thresholds and review cadence for MOC-related anomalies.",
      "href": "/docs/governance-moc"
    },
    {
      "title": "Top anomalies — MOC",
      "snippet": "Three recent cost spikes and recommended signals to emit.",
      "href": "/hubs/moc/anomalies"
    }
  ]
}
```

#### B) Card component
`src/components/cards/TopHubSignalCard.tsx`
```tsx
import { useEffect, useState } from 'react';
import { ExternalLink, TrendingUp, FileText } from 'lucide-react';

interface HubContext {
  title: string;
  snippet: string;
  href: string;
}

interface HubInsight {
  hub: string;
  connections: number;
  generatedAt: string;
  contexts: HubContext[];
}

export default function TopHubSignalCard() {
  const [insight, setInsight] = useState<HubInsight | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Read-only static fetch. Cache-bust on deploy via filename hash or query param if needed.
    fetch('/data/top-hub.json', { cache: 'no-store' })
      .then((r) => {
        if (!r.ok) throw new Error('Failed to load top-hub snapshot');
        return r.json();
      })
      .then((data) => {
        setInsight(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border bg-card p-6">
        <div className="h-5 w-32 animate-pulse rounded bg-muted" />
        <div className="mt-4 h-8 w-20 animate-pulse rounded bg-muted" />
      </div>
    );
  }

  if (!insight) {
    // Fail silent (read-only). Optionally show a minimal fallback.
    return null;
  }

  return (
    <div className="rounded-lg border bg-card p-6 shadow-sm">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <TrendingUp className="h-5 w-5 text-primary" />
          <h3 className="font-semibold">Top-Hub Signal</h3>
        </div>
        <span className="text-xs text-muted-foreground">Sense + Signal</span>
      </div>

      <div className="mt-4">
        <p className="text-sm text-muted-foreground">Most-connected hub</p>
        <p className="text-2xl font-bold">{insight.hub}</p>
        <p className="text-xs text-muted-foreground">
          {insight.connections.toLocaleString()} connections
        </p>
      </div>

      <div className="mt-4 space-y-2">
        <p className="text-xs font-medium text-muted-foreground">Top contexts</p>
        {insight.contexts.map((ctx, i) => (
          <a
            key={i}
            href={ctx.href}
            target="_blank"
            rel="noopener noreferrer"
            className="block rounded-md border p-2 text-sm hover:bg-accent"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-1">
                <FileText className="mt-0.5 h-3 w-3 flex-shrink-0 text-muted-foreground" />
                <span className="font-medium">{ctx.title}</span>
              </div>
              <ExternalLink className="mt-0.5 h-3 w-3 flex-shrink-0 text-muted-foreground" />
            </div>
            <p className="ml-5 mt-0.5 truncate text-xs text-muted-foreground">
              {ctx.snippet}
            </p>
          </a>
        ))}
      </div>

      <div className="mt-4">
        <a
          href="/hubs"
          className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
        >
          View hub details <ExternalLink className="h-3 w-3" />
        </a>
      </div>

      <div className="mt-3 text-xs text-muted-foreground">
        Snapshot: {new Date(insight.generatedAt).toLocaleDateString()}
      </div>
    </div>
  );
}
```

#### C) Mount on dashboard
`src/app/dashboard/page.tsx` (or equivalent)
```tsx
import TopHubSignalCard from '@/components/cards/TopHubSignalCard';

export default function DashboardPage() {
  return (
    <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3">
      {/* Existing cards ... */}
      <div className="xl:col-span-2">
        <TopHubSignalCard />
      </div>
      {/* Other cards */}
    </div>
  );
}
```

---

### 3) Optional: lightweight refresh script (ops)
`scripts/refresh-top-hub-snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail
# Refresh top-hub snapshot (read-only). Safe to run manually or via cron.
# Usage: bash scripts/refresh-top-hub-snapshot.sh

OUT_DIR="public/data"
OUT_FILE="${OUT_DIR}/top-hub.json"

mkdir -p "${OUT_DIR}"

# Replace this block with a real read-only knowledge-rag query if available.
# Example (read-only):
#   curl -sSf "https://knowledge-rag.local/api/top-hub?format=json" > "${OUT_FILE}.tmp" && mv "${OUT_FILE}.tmp" "${OUT_FILE}"
# For immediate delivery, produce deterministic stub:

cat > "${OUT_FILE}" <<'JSON'
{
  "hub": "MOC",
  "connections": 1247,
  "generatedAt": "2026-05-03T00:00:00Z",
  "contexts": [
    {
      "title": "MOC — Multi-Org Cost model",
      "snippet": "Describes cross-org cost allocation and chargeback patterns.",
      "href": "/docs/moc-cost-model"
    },
    {
      "title": "Governance playbook (MOC)",
      "snippet": "Signal thresholds and review cadence for MOC-related anomalies.",
      "href": "/docs/governance-moc"
   
