# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)

**Scope & Value**  
- Frontend-only, read-only React panel that surfaces the highest-signal hub (default “MOC”) and its cost-saving proposals from the knowledge graph.  
- CDN-first data delivery to eliminate Hugging Face API calls at runtime.  
- Single pre-built JSON embedded in the repo and mirrored to CDN; zero runtime API dependencies.  
- Reuses active Lightning Studio sessions when available; idle-aware restart for long-running ingestion/training.  
- Tags: `#knowledge-rag` `#graph` `#hub` `#cost-optimization` `#cdn` `#lightning-ai` `#quota-safe`

**Estimated effort**: <2 hours

---

### 1) Data contract (CDN-first, canonical)

File (committed + mirrored):  
`data/hubs/moc-proposals.json`

```json
{
  "hub": "MOC",
  "title": "Mission Operations Center",
  "updated": "2026-05-03T02:00:00Z",
  "proposals": [
    {
      "id": "moc-ri-2026-05",
      "type": "reserved_instance",
      "title": "RI coverage gap — us-east-1 m5.xlarge",
      "severity": "high",
      "confidence": 0.87,
      "estimated_savings_monthly_usd": 4200,
      "window": "2026-05-03..2026-05-10",
      "context": "32% on-demand usage in steady workload window",
      "proposal": "Purchase 1-year convertible RIs for m5.xlarge in us-east-1",
      "evidence": ["cost-anomaly-2026-05-02.jsonl"],
      "tags": ["RI", "AWS", "compute"],
      "actions": [
        { "label": "Open proposal", "href": "/proposals/moc-ri-2026-05" },
        { "label": "View analysis", "href": "/hubs/moc/analysis/ri-coverage" }
      ]
    },
    {
      "id": "moc-snapshot-orphan-2026-05",
      "type": "snapshot_orphan",
      "title": "Orphaned EBS snapshots (>30d)",
      "severity": "medium",
      "confidence": 0.79,
      "estimated_savings_monthly_usd": 860,
      "window": "2026-05-03..2026-05-10",
      "context": "47 snapshots; 1.2TB total; last attach >45d",
      "proposal": "Snapshot lifecycle policy: retain 30d, then archive to Glacier",
      "evidence": ["snapshot-inventory-2026-05-01.jsonl"],
      "tags": ["EBS", "AWS", "storage"],
      "actions": [
        { "label": "Review candidates", "href": "/hubs/moc/snapshots/orphaned" }
      ]
    }
  ]
}
```

Decisions (resolve contradictions):  
- Use `severity` (not `impact`) to align with common cost/ops dashboards and keep wording consistent.  
- Keep both `proposal` (recommended action) and `context` (why) for completeness and actionability.  
- Include `evidence` and `tags` for traceability and filtering without bloating UI logic.  
- Embed at build time; runtime fetch from `/data/hubs/moc-proposals.json` (CDN-backed) with `cache: 'no-store'` for freshness.

---

### 2) Pre-list & CDN fetch strategy (Mac orchestration)

Script: `scripts/list-hub-files.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# Single API call after rate-limit window; recursive=false per folder.
# Output consumed by build step and training script.
REPO="axentx/costinel-data"
FOLDER="hubs/moc"
OUT="data/file-list-moc.json"

mkdir -p "$(dirname "$OUT")"

python3 - <<PY
import json, os, sys
from huggingface_hub import list_repo_tree

REPO = os.getenv("REPO")
FOLDER = os.getenv("FOLDER")
OUT = os.getenv("OUT")

items = list_repo_tree(repo_id=REPO, path=FOLDER, recursive=False)
files = [
    {"path": f.path, "cdn_url": f"https://huggingface.co/datasets/{REPO}/resolve/main/{f.path}"}
    for f in items
    if f.type == "file"
]

with open(OUT, "w") as f:
    json.dump({"repo": REPO, "folder": FOLDER, "files": files}, f, indent=2)

print(f"Wrote {len(files)} files to {OUT}")
PY
```

- Make executable: `chmod +x scripts/list-hub-files.sh`  
- Cron: set `SHELL=/bin/bash` and invoke via `bash scripts/list-hub-files.sh` (avoids wrapper exec errors).  
- Run once per day after nightly graph ingestion to refresh file list.

---

### 3) Frontend panel (React/Next.js)

Component: `components/TopHubSignalPanel.tsx`

```tsx
'use client';

import { useEffect, useState } from 'react';
import { ExternalLink, DollarSign, AlertCircle, CheckCircle } from 'lucide-react';

interface Proposal {
  id: string;
  type: string;
  title: string;
  severity: 'high' | 'medium' | 'low';
  confidence: number;
  estimated_savings_monthly_usd: number;
  window: string;
  context: string;
  proposal: string;
  evidence: string[];
  tags: string[];
  actions: Array<{ label: string; href: string }>;
}

interface HubData {
  hub: string;
  title: string;
  updated: string;
  proposals: Proposal[];
}

const severityColors = {
  high: 'bg-red-50 border-red-200 text-red-800',
  medium: 'bg-amber-50 border-amber-200 text-amber-800',
  low: 'bg-emerald-50 border-emerald-200 text-emerald-800',
};

export default function TopHubSignalPanel({ initialData }: { initialData?: HubData }) {
  const [data, setData] = useState<HubData | null>(initialData || null);
  const [loading, setLoading] = useState(!initialData);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (initialData) return;
    fetch('/data/hubs/moc-proposals.json', { cache: 'no-store' })
      .then((r) => {
        if (!r.ok) throw new Error(`Failed to load hub data: ${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [initialData]);

  if (loading) return <div className="p-4 animate-pulse">Loading hub signals…</div>;
  if (error) return <div className="p-4 text-red-600">Error: {error}</div>;
  if (!data || !data.proposals.length) return null;

  return (
    <section aria-labelledby="top-hub-title" className="rounded-xl border bg-white p-5 shadow-sm">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 id="top-hub-title" className="text-lg font-semibold text-gray-900">
            Top Hub: {data.title} ({data.hub})
          </h2>
          <p className="text-sm text-gray-500">Actionable proposals from knowledge graph</p>
        </div>
        <time dateTime={data.updated} className="text-xs text-gray-400">
          Updated {new Date(data.updated).toLocaleDateString()}
        </time
