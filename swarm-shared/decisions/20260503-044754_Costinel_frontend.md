# Costinel / frontend

## Final Decision  
**CDN-first “Top Hub Signal Panel”** — ship in <2 hours as a frontend-only widget on Costinel’s dashboard.

- Fetch a small, versioned JSON from CDN (static origin or HuggingFace `resolve/main/`) with **no Authorization header** to bypass HF API rate limits.  
- Show the most-connected hub (e.g., MOC) + 3–5 actionable signals/docs.  
- Use strict timeouts, graceful fallback (inline default or last-known), manual refresh, and timestamp.  
- Zero backend changes and zero model compute at runtime.

---

## Implementation plan (frontend-only)

1) **Create static payload** (one-time, can be automated later)  
   - Path: `public/data/top-hub.json`  
   - Schema: `{ hub: { id, title, summary, url }, signals: Array<{ label, value, severity, url? }>, updatedAt, source }`

2) **Add TopHubPanel component**  
   - Location: `src/components/TopHubPanel.tsx`  
   - Fetch from `/data/top-hub.json` with `cache: 'no-store'` + `AbortController(4s)` + **no Authorization header**.  
   - Fallback to inline default JSON if fetch fails or times out.  
   - Manual refresh button and formatted timestamp.

3) **Mount in dashboard**  
   - Insert near the cost summary or sidebar (highest-traffic viewport).  
   - Use existing design tokens/spacing.

4) **Optional automation**  
   - Add a small script (`scripts/refresh-top-hub.sh`) to regenerate JSON from knowledge-rag/graph export and commit or upload to CDN.

---

## Code snippets

### 1) Static payload (commit to repo)

`public/data/top-hub.json`
```json
{
  "hub": {
    "id": "MOC",
    "title": "MOC — Most Connected Hub",
    "summary": "Mission Operations Center is the top hub by graph centrality this cycle.",
    "url": "/hubs/MOC"
  },
  "signals": [
    { "label": "Cost drift ↑", "value": "+12% WoW", "severity": "warning", "url": "/signals/cost-drift" },
    { "label": "Reserved coverage", "value": "68%", "severity": "info", "url": "/docs/RI-101" },
    { "label": "Anomalies", "value": "3 new", "severity": "critical", "url": "/anomalies" }
  ],
  "updatedAt": "2026-05-03T08:00:00.000Z",
  "source": "knowledge-rag#graph#top-hub"
}
```

---

### 2) TopHubPanel component

`src/components/TopHubPanel.tsx`
```tsx
import { useEffect, useState } from 'react';

interface Signal {
  label: string;
  value: string;
  severity: 'critical' | 'warning' | 'info';
  url?: string;
}

interface Hub {
  id: string;
  title: string;
  summary: string;
  url?: string;
}

interface TopHubPayload {
  hub: Hub;
  signals: Signal[];
  updatedAt: string;
  source: string;
}

const CDN_SIGNAL_PATH = '/data/top-hub.json';
const TIMEOUT_MS = 4000;

const DEFAULT_PAYLOAD: TopHubPayload = {
  hub: {
    id: 'MOC',
    title: 'MOC — Most Connected Hub',
    summary: 'Mission Operations Center is the top hub by graph centrality this cycle.',
    url: '/hubs/MOC'
  },
  signals: [
    { label: 'Cost drift ↑', value: '+12% WoW', severity: 'warning' },
    { label: 'Reserved coverage', value: '68%', severity: 'info' },
    { label: 'Anomalies', value: '3 new', severity: 'critical' }
  ],
  updatedAt: new Date().toISOString(),
  source: 'knowledge-rag#graph#top-hub'
};

export default function TopHubPanel() {
  const [data, setData] = useState<TopHubPayload>(DEFAULT_PAYLOAD);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);

    fetch(CDN_SIGNAL_PATH, { cache: 'no-store', signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((json) => {
        setData((prev) => ({ ...DEFAULT_PAYLOAD, ...json, signals: json.signals || DEFAULT_PAYLOAD.signals }));
      })
      .catch((err) => {
        if (err.name !== 'AbortError') {
          setError(err.message || 'Failed to load top hub');
          // keep DEFAULT_PAYLOAD visible
        }
      })
      .finally(() => {
        setLoading(false);
        clearTimeout(timeout);
      });

    return () => {
      controller.abort();
      clearTimeout(timeout);
    };
  };

  useEffect(() => {
    return load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleRefresh = () => load();

  const formatDate = (iso: string) =>
    new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

  return (
    <div className="rounded-lg border bg-card/50 p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">{data.hub.title}</h3>
          <p className="text-xs text-muted-foreground">{data.hub.summary}</p>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase text-muted-foreground/60">
            {formatDate(data.updatedAt)}
          </span>
          <button
            onClick={handleRefresh}
            disabled={loading}
            className="rounded p-1 text-xs text-muted-foreground hover:bg-accent disabled:opacity-50"
            aria-label="Refresh top hub signals"
          >
            {loading ? '…' : '↻'}
          </button>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {data.signals.map((s, i) => {
          const Comp = s.url ? 'a' : 'div';
          return (
            <Comp
              key={i}
              href={s.url}
              className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
                s.severity === 'critical'
                  ? 'bg-destructive/10 text-destructive'
                  : s.severity === 'warning'
                  ? 'bg-warning/10 text-warning'
                  : 'bg-info/10 text-info'
              } ${s.url ? 'hover:underline' : ''}`}
            >
              {s.label}: {s.value}
            </Comp>
          );
        })}
      </div>

      {error && <div className="mt-2 text-[11px] text-destructive">{error}</div>}
      <div className="mt-2 text-[10px] text-muted-foreground/50">{data.source}</div>
    </div>
  );
}
```

---

### 3) Mount in dashboard

Example placement in `src/pages/Dashboard.tsx` (adjust to actual layout):

```tsx
import TopHubPanel from '@/components/TopHubPanel';

export default function Dashboard() {
  return (
    <div className="grid gap-6">
      <div className="grid gap-4 md:grid-cols-3">
        {/*
