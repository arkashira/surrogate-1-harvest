# Costinel / quality

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top‑Hub Signal Panel to the Costinel dashboard that surfaces the most‑connected hub (e.g., "MOC") using CDN‑first data baked at build time (zero HuggingFace API calls at runtime).

### High-level approach
1. **Backend** (build-time): generate `top-hub.json` from knowledge-rag output and commit to `public/data/top-hub.json` (or serve via CDN path).
2. **Frontend**: lightweight panel component that fetches `/data/top-hub.json` (CDN) and renders signal + context.
3. **Ops**: ensure build script is idempotent and cron-friendly; no runtime HF API usage.

Estimated effort: ~90 minutes.

---

### 1) Build-time generator (backend)

Create `scripts/generate-top-hub.js` (Node, runs in CI/build):

```bash
#!/usr/bin/env node
/**
 * Generate top-hub.json for Costinel dashboard.
 * Uses local knowledge-rag output or cached artifacts.
 * Output: public/data/top-hub.json
 */

const fs = require('fs');
const path = require('path');

// If you have a local CLI for knowledge-rag, invoke it here.
// For now we produce a deterministic stub from repo metadata + timestamp.
function collectTopHub() {
  // In production replace this with:
  // const { execSync } = require('child_process');
  // const out = execSync('knowledge-rag --top-hub --json', { encoding: 'utf8' });
  // return JSON.parse(out);

  return {
    hub: 'MOC',
    title: 'Mission Operations Center',
    score: 0.94,
    connections: 128,
    summary:
      'Central hub for mission telemetry, anomaly detection, and operator workflows. Highest betweenness centrality in the Costinel knowledge graph.',
    signals: [
      {
        id: 'cost-anomaly-moc-001',
        type: 'cost_spike',
        severity: 'medium',
        description: 'Intermittent compute bursts linked to MOC batch jobs.',
        recommendation: 'Shift non-urgent workloads to off-peak windows.',
      },
      {
        id: 'ri-coverage-moc-002',
        type: 'ri_coverage',
        severity: 'low',
        description: 'Steady baseline usage suitable for 1-yr RI coverage.',
        recommendation: 'Purchase 1-yr RIs for baseline MOC capacity.',
      },
    ],
    updatedAt: new Date().toISOString(),
  };
}

function main() {
  const outDir = path.resolve(__dirname, '..', 'public', 'data');
  if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });

  const payload = collectTopHub();
  const outPath = path.join(outDir, 'top-hub.json');
  fs.writeFileSync(outPath, JSON.stringify(payload, null, 2), 'utf8');
  console.log(`[top-hub] written ${outPath}`);
}

if (require.main === module) {
  main();
}
```

Make executable and add to package.json build script:

```json
{
  "scripts": {
    "build:top-hub": "node scripts/generate-top-hub.js",
    "build": "npm run build:top-hub && next build"
  }
}
```

---

### 2) Frontend panel component

Create `components/TopHubSignalPanel.tsx`:

```tsx
'use client';

import { useEffect, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { AlertCircle, TrendingUp, Calendar } from 'lucide-react';

interface Signal {
  id: string;
  type: string;
  severity: 'low' | 'medium' | 'high';
  description: string;
  recommendation: string;
}

interface TopHubData {
  hub: string;
  title: string;
  score: number;
  connections: number;
  summary: string;
  signals: Signal[];
  updatedAt: string;
}

const severityColor = {
  low: 'bg-green-100 text-green-800',
  medium: 'bg-yellow-100 text-yellow-800',
  high: 'bg-red-100 text-red-800',
};

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // CDN-first fetch — no auth, no HF API calls at runtime.
    fetch('/data/top-hub.json', { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error('Failed to load top-hub data');
        return res.json();
      })
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <Card>
        <CardContent className="p-6">
          <div className="animate-pulse h-20 bg-muted rounded" />
        </CardContent>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card>
        <CardContent className="p-6 text-sm text-muted-foreground">
          Top-hub signals unavailable.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between pb-3">
        <div className="space-y-1">
          <CardTitle className="text-lg flex items-center gap-2">
            <TrendingUp className="h-5 w-5 text-primary" />
            Top Hub: {data.hub}
          </CardTitle>
          <p className="text-sm text-muted-foreground">{data.title}</p>
        </div>
        <Badge variant="outline" className="font-mono">
          {data.score.toFixed(2)}
        </Badge>
      </CardHeader>

      <CardContent className="space-y-4">>
        <p className="text-sm text-muted-foreground">{data.summary}</p>

        <div className="flex items-center gap-1 text-xs text-muted-foreground">
          <Calendar className="h-3 w-3" />
          {new Date(data.updatedAt).toLocaleString()}
          <span className="ml-auto">{data.connections} connections</span>
        </div>

        <div className="space-y-2">
          {data.signals.map((s) => (
            <div
              key={s.id}
              className="border rounded-md p-3 bg-muted/30 space-y-2"
            >
              <div className="flex items-center justify-between">
                <span className="text-xs font-mono text-muted-foreground">
                  {s.id}
                </span>
                <Badge className={severityColor[s.severity]}>
                  {s.severity}
                </Badge>
              </div>
              <p className="text-sm font-medium">{s.description}</p>
              <p className="text-xs text-muted-foreground">
                Recommendation: {s.recommendation}
              </p>
            </div>
          ))}
        </div>

        <p className="text-xs text-muted-foreground/60">
          Note: Costinel senses and signals — does not execute changes.
        </p>
      </CardContent>
    </Card>
  );
}
```

---

### 3) Add panel to dashboard

Edit `app/dashboard/page.tsx` (or wherever the main dashboard lives) and insert:

```tsx
import TopHubSignalPanel from '@/components/TopHubSignalPanel';

export default function DashboardPage() {
  return (
    <div className="grid gap-6">
      {/* Existing widgets ... */}
      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">

