# Costinel / quality

### Final Synthesis (merged + corrected)

**Highest-value incremental improvement (<2h): Add a read-only Top-Hub Signal Panel to Costinel that surfaces the most-connected hub (default “MOC”) and its top 3 cost-impact proposals — CDN-first, zero runtime API calls, rate-limit-safe, no secrets.**

---

### Why this wins
- **Correctness**: Uses static/public assets served from `/public/knowledge/signals/` (or CDN mirror) so there are **zero runtime API calls and no Authorization headers** — eliminating auth leaks and rate-limit risk.
- **Actionability**: Generates a real signal file once (Mac/CI) and commits it; the dashboard reads it as a plain static fetch. No backend changes, no secrets, minimal footprint.
- **UX + reliability**: Read-only panel with graceful fallback states (loading, error, empty). Uses existing design tokens (shadcn/ui + Tailwind) and compact impact badges.
- **CDN-ready**: Path `/knowledge/signals/top-hub-moc.json` maps to `public/knowledge/signals/...` locally and can be mirrored on HF CDN without auth.

---

### Implementation plan (prioritized)

1. **Generate signal file (once, on Mac/CI)**  
   - Run a one-off script that uses `knowledge-rag` (or repo tree) to identify the top hub (degree centrality) and export top 3 signals to `/public/knowledge/signals/top-hub-moc.json`.  
   - Commit file (or upload to CDN mirror).

2. **Add static asset path**  
   - Ensure file is in `/public/knowledge/signals/top-hub-moc.json` so Next.js serves it as `/knowledge/signals/top-hub-moc.json` (public route).

3. **Create React component** (`TopHubSignalPanel`)  
   - Fetch `/knowledge/signals/top-hub-moc.json` at runtime with `credentials: 'omit'`.  
   - Render hub name, updated date, and top 3 signals as compact cards with impact badges.  
   - Graceful fallback UI for loading/error/empty.

4. **Wire into dashboard**  
   - Insert `<TopHubSignalPanel />` near the top of the dashboard (below header or in summary area).

5. **Styling & polish**  
   - Use existing shadcn/ui components (`Card`, `Badge`, `Alert`) and Tailwind spacing.

6. **Verify CDN-only behavior**  
   - Confirm no `Authorization` header is sent (browser default).  
   - Confirm file reachable at CDN URL when mirrored (e.g., Hugging Face datasets resolve).

---

### Corrected, production-ready code

#### 1) Example signal file (committed or mirrored)

`/public/knowledge/signals/top-hub-moc.json`
```json
{
  "hub": "MOC",
  "updatedAt": "2026-05-03T02:30:00Z",
  "signals": [
    {
      "title": "Unattached EBS volumes in us-east-1",
      "impact": "High",
      "context": "12 unattached gp3 volumes (~$180/mo). Recommend snapshot + delete after 7d retention.",
      "cdnPath": "knowledge/graph/moc/ebs-unattached-2026-05-03.md"
    },
    {
      "title": "Over-provisioned RDS db.m6g.2xlarge",
      "impact": "Medium",
      "context": "CPU avg 18% over 14d. Downsize to db.m6g.xlarge saves ~$210/mo.",
      "cdnPath": "knowledge/graph/moc/rds-overprovision-2026-05-03.md"
    },
    {
      "title": "Idle NAT gateways (2) across prod accounts",
      "impact": "Medium",
      "context": "No traffic in 30d. Removal saves ~$105/mo.",
      "cdnPath": "knowledge/graph/moc/nat-idle-2026-05-03.md"
    }
  ]
}
```

#### 2) React component (TypeScript)

`components/TopHubSignalPanel.tsx`
```tsx
'use client';

import { useEffect, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { FileText, Clock, AlertCircle } from 'lucide-react';

interface Signal {
  title: string;
  impact: 'High' | 'Medium' | 'Low';
  context: string;
  cdnPath: string;
}

interface TopHubData {
  hub: string;
  updatedAt: string;
  signals: Signal[];
}

const impactColors = {
  High: 'destructive',
  Medium: 'warning',
  Low: 'secondary',
} as const;

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // CDN-first, public static file. credentials: 'omit' avoids sending auth headers.
    fetch('/knowledge/signals/top-hub-moc.json', { credentials: 'omit' })
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load signals: ${res.status}`);
        return res.json();
      })
      .then((json) => {
        // Basic runtime validation
        if (!json || typeof json.hub !== 'string' || !Array.isArray(json.signals)) {
          throw new Error('Invalid signal data format');
        }
        setData(json);
      })
      .catch((err) => setError(err.message));
  }, []);

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertCircle className="h-4 w-4" />
        <AlertDescription>Could not load top-hub signals.</AlertDescription>
      </Alert>
    );
  }

  if (!data) {
    return (
      <Card>
        <CardContent className="p-6">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Clock className="h-4 w-4" />
            Loading top-hub signals...
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between">
          <span className="flex items-center gap-2">
            <FileText className="h-5 w-5" />
            Top-Hub: {data.hub}
          </span>
          <Badge variant="outline" className="text-xs">
            Updated {new Date(data.updatedAt).toLocaleDateString()}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex flex-col gap-3">
          {data.signals.slice(0, 3).map((s, i) => (
            <div
              key={i}
              className="flex flex-col gap-1 rounded-lg border bg-card p-3 text-sm"
            >
              <div className="flex items-start justify-between gap-2">
                <span className="font-medium leading-tight">{s.title}</span>
                <Badge variant={impactColors[s.impact]} className="text-xs shrink-0">
                  {s.impact}
                </Badge>
              </div>
              <p className="text-muted-foreground mt-1 leading-tight">{s.context}</p>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
```

#### 3) One-off generator script (run locally/CI to produce the JSON)

`scripts/generate-top-hub-signals.js` (Node example — adapt to Python if preferred)
```js
// scripts/generate-top-hub-signals.js
// Run: node scripts/generate-top-hub-signals.js
// Produces: public/knowledge/signals/top-hub-moc.json

const fs = require('fs');
const path = require('path');

// Mocked example generator
