# Costinel / frontend

## Final Synthesis & Action Plan  

**Core decision (unified):**  
Ship a **frontend-only “Top-Hub Signal” card** that surfaces the most-connected hub (e.g., “MOC”) and 3–5 related docs with short, actionable context.  
- No backend/API/auth changes.  
- Uses existing RAG pipeline (`granite-business-research.sh` + `knowledge-rag`) to produce a static JSON artifact consumed by the UI.  
- Fits Costinel philosophy: **Sense + Signal (no Execute)**.  
- Target: ≤2h.

---

## 1) Data contract (static artifact)

File: `src/data/top-hub-signal.json`

```json
{
  "hub": "MOC",
  "rank": 1,
  "summary": "Multi-Org Cost (MOC) is the most-connected hub in Costinel's knowledge graph. Centralizes cross-account/cross-cloud cost visibility and governance signals for enterprise orgs.",
  "relatedDocs": [
    {
      "title": "Cost Anomaly Detection Patterns",
      "url": "/docs/cost-anomalies",
      "snippet": "Detect and signal spend spikes across org units."
    },
    {
      "title": "RI Coverage & Commitment Playbook",
      "url": "/docs/ri-playbook",
      "snippet": "Actionable guidance for reserved capacity planning."
    },
    {
      "title": "Governance Workflow & Approvals",
      "url": "/docs/governance-workflow",
      "snippet": "How proposals flow from signal to human review."
    }
  ],
  "updatedAt": "2026-05-03T00:00:00.000Z"
}
```

- **Why this shape:** Combines Candidate 1’s clarity (`hub`, `summary`, `relatedDocs[]`, `updatedAt`) with Candidate 2’s `rank` for future sorting/expansion.  
- **Correctness:** `relatedDocs` is an array of objects with `title`, `url`, `snippet`. Keep `url` as relative or absolute path so links work in the deployed app.

---

## 2) Component (concrete, accessible, actionable)

File: `src/components/costintel/TopHubSignalCard.tsx`

```tsx
import React from 'react';
import { ExternalLink } from 'lucide-react';
import topHubSignal from '../../data/top-hub-signal.json';

interface RelatedDoc {
  title: string;
  url: string;
  snippet: string;
}

interface TopHubSignal {
  hub: string;
  rank: number;
  summary: string;
  relatedDocs: RelatedDoc[];
  updatedAt: string;
}

const TopHubSignalCard: React.FC = () => {
  const signal = topHubSignal as TopHubSignal;

  return (
    <section
      className="rounded-lg border bg-card p-5 shadow-sm"
      aria-labelledby="top-hub-title"
    >
      <div className="mb-3 flex items-start justify-between">
        <div>
          <h2 id="top-hub-title" className="text-lg font-semibold">
            Top Hub: {signal.hub}
          </h2>
          <p className="text-xs text-muted-foreground">
            Updated {new Date(signal.updatedAt).toLocaleString()}
          </p>
        </div>
        <span className="inline-flex items-center rounded bg-muted px-2 py-0.5 text-xs font-medium">
          Rank {signal.rank}
        </span>
      </div>

      <p className="mb-4 text-sm text-muted-foreground">{signal.summary}</p>

      <div className="space-y-2">
        <h3 className="text-sm font-medium">Related docs</h3>
        <ul className="space-y-2" aria-label="Related documents">
          {signal.relatedDocs.map((doc, idx) => (
            <li key={idx} className="flex gap-2 text-sm">
              <a
                href={doc.url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex-1 text-foreground hover:underline"
              >
                {doc.title}
              </a>
              <ExternalLink className="h-4 w-4 text-muted-foreground flex-shrink-0" />
              <span className="text-muted-foreground line-clamp-2 flex-1">{doc.snippet}</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
};

export default TopHubSignalCard;
```

- **Accessibility:** `section` with `aria-labelledby`, semantic list, clear link labels.  
- **Actionability:** Links open in new tab (`target="_blank"`, `rel="noopener noreferrer"`). Snippets shown inline for quick context.  
- **Graceful degradation:** If JSON is missing/empty, the component will throw at runtime during dev; in production, consider a safe fallback (e.g., `try/catch` import or optional chaining) if you want zero-runtime-fail behavior.

---

## 3) Dashboard integration

File: `src/pages/Dashboard.tsx` (or equivalent)

```tsx
import React from 'react';
import TopHubSignalCard from '../components/costintel/TopHubSignalCard';

const Dashboard: React.FC = () => {
  return (
    <main className="p-6">
      <div className="grid gap-6 lg:grid-cols-3">
        {/* Existing analytics cards */}
        <div className="lg:col-span-2 space-y-6">
          {/* ... existing cost analytics cards ... */}
        </div>

        {/* Sidebar column */}
        <aside className="space-y-6">
          <TopHubSignalCard />
          {/* Other sidebar widgets */}
        </aside>
      </div>
    </main>
  );
};

export default Dashboard;
```

- **Placement:** Sidebar column keeps the card visible without crowding primary analytics. Adjust grid spans to match your layout.

---

## 4) Refresh script (dev/ops)

File: `scripts/refresh-top-hub.sh`

```bash
#!/usr/bin/env bash
# scripts/refresh-top-hub.sh
# Regenerate top-hub signal from RAG pipeline.
# Requires: granite-business-research.sh and knowledge-rag available.

set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

# 1) Run business research
echo "Running granite-business-research.sh..."
bash granite-business-research.sh

# 2) Query top hub and related docs via knowledge-rag
echo "Querying knowledge-rag for top hub and related docs..."
TOP_HUB=$(knowledge-rag --query "top hub" --format json | jq -r '.hub // "MOC"')
RELATED_JSON=$(knowledge-rag --query "related docs for ${TOP_HUB}" --format json | jq '.docs // []')

# 3) Build JSON payload
UPDATED_AT=$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")

cat > src/data/top-hub-signal.json <<EOF
{
  "hub": "${TOP_HUB}",
  "rank": 1,
  "summary": "Auto-generated top hub from RAG pipeline.",
  "relatedDocs": $(echo "$RELATED_JSON" | jq '
    map({
      title: .title // "Untitled",
      url: .url // "#",
      snippet: .snippet // ""
    })
  '),
  "updatedAt": "${UPDATED_AT}"
}
EOF

echo "Updated src/data/top-hub-signal.json (hub=${TOP_HUB})"
```

Make executable:

```bash
chmod +x scripts/refresh-top-hub.sh
```

Add to `package.json` (optional):

```json
"scripts": {
  "refresh:top-hub": "bash scripts/refresh-top-hub.sh"
}
```

- **Correctness + safety:** Uses `jq` to normalize fields and provide defaults so malformed RAG output doesn’t break the JSON contract.  
- **Actionability:** Run manually during builds or via cron/CI to keep the artifact fresh. For production automation later, move this to a small backend job that writes to a DB or serves via an API.

---

## 5
