# Costinel / frontend

## Final Implementation Plan  
CDN-First Top-Hub Signal Panel — zero runtime HF API, <2h, production-ready.

---

### 1. Architecture (merged best parts)
- **CDN-first**: static JSON baked at build/CI and served as a public asset.  
- **Zero runtime HF calls**: dashboard fetches `/data/top-hub.json` (relative public path).  
- **Non-blocking**: panel renders nothing while loading or on failure; never flashes errors or blocks UI.  
- **Graceful staleness policy**: if data missing or older than 7 days, panel hides itself (non-blocking fallback).  
- **Design consistency**: uses existing tokens (`bg-card`, `border`, `text-foreground`, `text-muted-foreground`, `rounded-lg`, `shadow-sm`).

---

### 2. File layout
```
/opt/axentx/Costinel/
├── public/
│   └── data/
│       └── top-hub.json          ← baked at build/CI
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel.tsx
│   ├── hooks/
│   │   └── useTopHubSignal.ts
│   ├── pages/
│   │   └── Dashboard.tsx
│   └── App.tsx
├── scripts/
│   └── build-top-hub-json.js     ← optional local/CI helper
├── package.json
└── README.md
```

---

### 3. Build-time data bake (one source of truth)
`public/data/top-hub.json` (committed or generated in CI):

```json
{
  "hub": "MOC",
  "title": "Most-Connected Hub",
  "score": 94.2,
  "trend": "up",
  "insight": "MOC drives 38% of cross-service signals this cycle. Prioritize RI coverage for linked compute workloads.",
  "updatedAt": "2026-05-03T03:35:04Z",
  "sourceUrl": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/moc-2026-05-03.json"
}
```

**CI generation (optional)**  
Run on orchestrator (Mac runner) after rate-limit window:

```bash
#!/usr/bin/env bash
set -euo pipefail

# One HF API call (or local logic) to determine top hub for today
# Replace stub with real query when available
DATE_FOLDER=$(date +%Y-%m-%d)
OUTPUT="public/data/top-hub.json"

mkdir -p "$(dirname "$OUTPUT")"

cat > "$OUTPUT" <<EOF
{
  "hub": "MOC",
  "title": "Most-Connected Hub",
  "score": 94.2,
  "trend": "up",
  "insight": "MOC drives 38% of cross-service signals this cycle. Prioritize RI coverage for linked compute workloads.",
  "updatedAt": "${DATE_FOLDER}T03:35:04Z",
  "sourceUrl": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/top-hub/moc-${DATE_FOLDER}.json"
}
EOF
```

**Acceptance**: file must be valid JSON and committed/published to CDN.

---

### 4. Hook: CDN-first fetch with staleness guard
`src/hooks/useTopHubSignal.ts`

```ts
const CDN_TOP_HUB = '/data/top-hub.json';
const STALE_DAYS = 7;

export interface TopHubSignal {
  hub: string;
  title: string;
  score: number;
  trend: 'up' | 'down' | 'flat';
  insight: string;
  updatedAt: string;
  sourceUrl?: string;
}

function isStale(updatedAt: string): boolean {
  try {
    const updated = new Date(updatedAt).getTime();
    const now = Date.now();
    return now - updated > STALE_DAYS * 24 * 60 * 60 * 1000;
  } catch {
    return true;
  }
}

export function useTopHubSignal() {
  const [signal, setSignal] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    setLoading(true);

    fetch(CDN_TOP_HUB, { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
        return res.json();
      })
      .then((data: TopHubSignal) => {
        if (!mounted) return;
        // Validate minimal shape
        if (!data.hub || typeof data.score !== 'number') throw new Error('Invalid payload');
        if (isStale(data.updatedAt)) throw new Error('Stale data');
        setSignal(data);
      })
      .catch(() => {
        // Silent, non-blocking fallback
        setSignal(null);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  return { signal, loading };
}
```

---

### 5. Component: subtle, non-blocking, accessible
`src/components/TopHubSignalPanel.tsx`

```tsx
import { TrendingUp, TrendingDown, Info } from 'lucide-react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';

export function TopHubSignalPanel() {
  const { signal, loading } = useTopHubSignal();

  if (loading || !signal) return null;

  const TrendIcon = signal.trend === 'up' ? TrendingUp : signal.trend === 'down' ? TrendingDown : null;

  return (
    <section
      aria-label={`Top hub signal: ${signal.hub}`}
      className="rounded-lg border bg-card p-4 shadow-sm"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-foreground">{signal.title}</h3>
            {TrendIcon && (
              <span className={signal.trend === 'up' ? 'text-green-600' : 'text-red-600'}>
                <TrendIcon size={16} />
              </span>
            )}
          </div>
          <p className="text-2xl font-bold text-foreground">{signal.hub}</p>
          <p className="text-xs text-muted-foreground">Score: {signal.score.toFixed(1)}</p>
        </div>
        <div className="shrink-0">
          <Info size={18} className="text-muted-foreground" />
        </div>
      </div>

      <p className="mt-3 text-sm text-muted-foreground">{signal.insight}</p>

      <footer className="mt-3 text-xs text-muted-foreground">
        Updated {new Date(signal.updatedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
      </footer>
    </section>
  );
}
```

---

### 6. Mount into Dashboard (non-blocking placement)
`src/pages/Dashboard.tsx`

```tsx
import { TopHubSignalPanel } from '../components/TopHubSignalPanel';

export default function Dashboard() {
  return (
    <main className="flex flex-col gap-6 p-6">
      <div className="grid gap-6 md:grid-cols-3">
        <aside className="md:col-span-1">
          <TopHubSignalPanel />
        </aside>

        <section className="md:col-span-2 space-y-6">
          {/* Existing cost analytics cards */}
        </section>
      </div>
    </main>
  );
}
```

---

### 7. Styling & behavior notes
- Uses existing design tokens; no new CSS variables required.  
- Panel is visually subtle and fits sidebar/header layouts.  
- Non-blocking:
