# Costinel / discovery

## Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & value**  
- Frontend-only, read-only panel that surfaces the most-connected hub (default “MOC”) and its actionable proposals from the knowledge graph.  
- CDN-first data delivery to bypass HF API limits and avoid runtime model loads.  
- Resilient, cache-first UX with graceful fallback and zero backend changes.  
- Ships in <2h.

---

### 1) Data strategy (CDN-bypass, no HF API at runtime)
- Single Mac-side script (run after `granite-business-research.sh`) calls `list_repo_tree` once for the date folder, saves `hub-signals.json` to repo.  
- `hub-signals.json` is committed and served via CDN (`https://huggingface.co/datasets/.../resolve/main/.../hub-signals.json`).  
- Frontend fetches CDN URL directly (no Authorization header) → avoids 429/1000 req limit.  
- Fallback: if CDN fails, load local copy from repo (`/data/hub-signals.json`).

**File schema (minimal)**  
```json
{
  "generatedAt": "2026-04-29T12:00:00Z",
  "topHub": "MOC",
  "hubLabel": "Mission Operations Center",
  "score": 0.92,
  "signals": [
    {
      "id": "moc-ri-2026-04",
      "title": "RI coverage gap in us-east-1",
      "type": "recommendation",
      "severity": "high",
      "impactUSD": 42000,
      "actions": ["Purchase 1yr No Upfront r6g.xlarge", "Enable auto-renew"],
      "evidence": ["ec2_running_instances_by_family", "ri_coverage_report"]
    }
  ]
}
```

---

### 2) File layout (additions only)
```
/opt/axentx/Costinel/
├── public/
│   └── favicon.svg
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel.jsx   ← new
│   ├── hooks/
│   │   └── useHubSignals.js        ← new
│   └── App.jsx                     ← import panel
├── data/
│   └── hub-signals.json            ← committed by devops (CDN mirror)
└── scripts/
    └── export-hub-signals.js       ← Mac orchestration helper
```

---

### 3) Implementation steps (concrete)

#### Step A — Export helper (run on Mac after research script)
`scripts/export-hub-signals.js`
```js
#!/usr/bin/env node
// One-time Mac orchestration script.
// Requires huggingface_hub (pip) or uses HF CLI to list tree and produce JSON.
// This script is run locally and commits data/hub-signals.json.

const fs = require('fs');
const path = require('path');

// Placeholder: in practice this calls HF API (list_repo_tree) once
// and produces minimal hub-signals.json. For now, emit sample.
const payload = {
  generatedAt: new Date().toISOString(),
  topHub: "MOC",
  hubLabel: "Mission Operations Center",
  score: 0.92,
  signals: [
    {
      id: "moc-ri-2026-04",
      title: "RI coverage gap in us-east-1",
      type: "recommendation",
      severity: "high",
      impactUSD: 42000,
      actions: ["Purchase 1yr No Upfront r6g.xlarge", "Enable auto-renew"],
      evidence: ["ec2_running_instances_by_family", "ri_coverage_report"]
    },
    {
      id: "moc-savingsplan-2026-04",
      title: "Savings Plan underutilization",
      type: "recommendation",
      severity: "medium",
      impactUSD: 18000,
      actions: ["Switch 30% of SP to Compute SP for flexibility"],
      evidence: ["savings_plan_utilization"]
    }
  ]
};

const outDir = path.join(__dirname, '..', 'data');
if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(
  path.join(outDir, 'hub-signals.json'),
  JSON.stringify(payload, null, 2)
);
console.log('Exported data/hub-signals.json');
```
Make executable and run once:
```bash
chmod +x scripts/export-hub-signals.js
node scripts/export-hub-signals.js
git add data/hub-signals.json && git commit -m "data: add hub-signals for MOC"
```

#### Step B — Hook: CDN-first fetch with cache + fallback
`src/hooks/useHubSignals.js`
```js
import { useEffect, useState } from 'react';

const CDN_URL = 'https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/data/hub-signals.json';
const LOCAL_URL = '/data/hub-signals.json';

export function useHubSignals() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;

    async function fetchFromCDN() {
      try {
        const res = await fetch(CDN_URL, { cache: 'force-cache' });
        if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
        const json = await res.json();
        if (!cancelled) {
          setData(json);
          setLoading(false);
        }
      } catch (err) {
        // fallback to local
        try {
          const res2 = await fetch(LOCAL_URL, { cache: 'no-cache' });
          if (!res2.ok) throw new Error(`Local fallback failed: ${res2.status}`);
          const json2 = await res2.json();
          if (!cancelled) {
            setData(json2);
            setLoading(false);
          }
        } catch (err2) {
          if (!cancelled) {
            setError(err2);
            setLoading(false);
          }
        }
      }
    }

    fetchFromCDN();
    return () => { cancelled = true; };
  }, []);

  return { data, loading, error };
}
```

#### Step C — Panel component (read-only, actionable signals)
`src/components/TopHubSignalPanel.jsx`
```jsx
import React from 'react';
import { useHubSignals } from '../hooks/useHubSignals';

const severityColor = (s) => {
  switch (s) {
    case 'high': return 'bg-red-50 border-red-200 text-red-800';
    case 'medium': return 'bg-amber-50 border-amber-200 text-amber-800';
    default: return 'bg-blue-50 border-blue-200 text-blue-800';
  }
};

export default function TopHubSignalPanel() {
  const { data, loading, error } = useHubSignals();

  if (loading) {
    return (
      <div className="p-4 border border-gray-100 rounded-lg bg-gray-50">
        <p className="text-sm text-gray-500">Loading top-hub signals…</p>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="p-4 border border-gray-100 rounded-lg bg-gray-50">
        <p className="text-sm text-gray-500">Signals unavailable.</p>
      </div>
    );
  }

  const { topHub, hubLabel, score, signals } = data;

  return (
    <div className="p-4 border border-gray-200 rounded-lg bg-white shadow-sm">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="text-base font-semibold text-gray-900">{hubLabel}</h3
