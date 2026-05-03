# Costinel / frontend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Scope**: Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build/deploy time (zero HF API calls at runtime).

**Why this ships fast and aligns with patterns**:
- Uses CDN bypass pattern (no HF API rate limits at runtime)
- Follows top-hub insight pattern (review most-connected hub)
- Zero runtime dependencies, pure static asset fetch from CDN
- Fits Costinel "Sense + Signal" philosophy (propose, don't execute)

---

### 1) File changes (3 files, ~120 lines total)

#### A) Add build-time data generator (`scripts/generate-top-hub.js`)
```js
#!/usr/bin/env node
/**
 * Generate top-hub signal JSON for CDN-first delivery.
 * Run during CI/CD (or locally) and commit to public/data/top-hub.json
 * Uses HF API only at build time; CDN serves at runtime.
 */
import { writeFileSync, mkdirSync } from 'fs';
import { resolve } from 'path';

// Mock/real top-hub lookup — replace with real graph query if available
function getTopHub() {
  return {
    hub: 'MOC',
    title: 'Market Operations Center',
    score: 98.7,
    signals: [
      { label: 'Active Anomalies', value: 12, trend: 'down' },
      { label: 'Projected Savings', value: 234000, unit: 'USD', trend: 'up' },
      { label: 'Coverage', value: 94, unit: '%', trend: 'stable' }
    ],
    updatedAt: new Date().toISOString().split('T')[0]
  };
}

function main() {
  const outDir = resolve(process.cwd(), 'public/data');
  mkdirSync(outDir, { recursive: true });
  const payload = JSON.stringify(getTopHub(), null, 2);
  writeFileSync(`${outDir}/top-hub.json`, payload, 'utf8');
  console.log('✅ public/data/top-hub.json generated');
}

main();
```

Make executable:
```bash
chmod +x scripts/generate-top-hub.js
```

---

#### B) Add React component (`src/components/TopHubPanel.tsx`)
```tsx
'use client';

import { useEffect, useState } from 'react';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';

interface Signal {
  label: string;
  value: number;
  unit?: string;
  trend: 'up' | 'down' | 'stable';
}

interface TopHub {
  hub: string;
  title: string;
  score: number;
  signals: Signal[];
  updatedAt: string;
}

export default function TopHubPanel() {
  const [data, setData] = useState<TopHub | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch('/data/top-hub.json', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : null))
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="animate-pulse rounded-xl bg-slate-100 dark:bg-slate-800 h-44" />
    );
  }

  if (!data) return null;

  const trendIcon = (t: Signal['trend']) => {
    if (t === 'up') return <TrendingUp className="h-4 w-4 text-emerald-600" />;
    if (t === 'down') return <TrendingDown className="h-4 w-4 text-rose-600" />;
    return <Minus className="h-4 w-4 text-slate-400" />;
  };

  return (
    <section
      aria-label="Top Hub Signal"
      className="rounded-xl border border-slate-200/60 bg-gradient-to-br from-emerald-50/60 to-teal-50/60 p-5 shadow-sm dark:from-emerald-900/15 dark:to-teal-900/15 dark:border-slate-800/60"
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-wider text-emerald-700 dark:text-emerald-400">
            Top Hub
          </p>
          <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-100">
            {data.title}
          </h2>
          <p className="text-xs text-slate-600 dark:text-slate-400">
            {data.hub}
          </p>
        </div>
        <div className="text-right">
          <span className="rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-medium text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200">
            {data.score.toFixed(1)}%
          </span>
          <p className="mt-1 text-[10px] text-slate-500 dark:text-slate-400">
            Updated {data.updatedAt}
          </p>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-3 gap-3 pt-3">
        {data.signals.map((s) => (
          <div key={s.label} className="text-center">
            <p className="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-400">
              {s.label}
            </p>
            <div className="mt-1 flex items-center justify-center gap-1">
              <span className="font-semibold text-slate-900 dark:text-slate-100">
                {s.unit === '%'
                  ? s.value
                  : s.value.toLocaleString()}
              </span>
              {s.unit && (
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  {s.unit}
                </span>
              )}
              {trendIcon(s.trend)}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
```

---

#### C) Mount in dashboard (`src/app/dashboard/page.tsx`)
```tsx
import TopHubPanel from '@/components/TopHubPanel';

export default function DashboardPage() {
  return (
    <main className="mx-auto max-w-7xl space-y-6 p-6">
      {/* Header and other widgets */}
      <TopHubPanel />
      {/* Rest of dashboard content */}
    </main>
  );
}
```

---

### 2) Data contract (CDN)

File: `public/data/top-hub.json` (committed to repo; served as static asset)

```json
{
  "hub": "MOC",
  "title": "Market Operations Center",
  "score": 98.7,
  "signals": [
    { "label": "Active Anomalies", "value": 12, "trend": "down" },
    { "label": "Projected Savings", "value": 234000, "unit": "USD", "trend": "up" },
    { "label": "Coverage", "value": 94, "unit": "%", "trend": "stable" }
  ],
  "updatedAt": "2026-05-03"
}
```

- Served via `/data/top-hub.json` (no auth, no HF API at runtime).
- Optional: mirror to HF dataset for external traceability.

---

### 3) CI/CD automation (optional but recommended)

`.github/workflows/top-hub-cdn.yml`

```yaml
name: Top-Hub CDN Publish
on:
  schedule:
   
