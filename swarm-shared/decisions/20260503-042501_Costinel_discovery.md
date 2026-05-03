# Costinel / discovery

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Ship a resilient “Top Hub” signal panel into Costinel that shows the most-connected hub (e.g., “MOC”) and related docs using CDN-fetched artifacts (zero runtime HF API calls). Aligns with Costinel philosophy: *Sense + Signal — ไม่ Execute*.

### Scope (≤2h)
- Add build-time fetch script (`scripts/fetch-top-hub.sh`) that:
  - Uses HF CDN (`https://huggingface.co/datasets/.../resolve/main/...`) to download a small JSON artifact listing top hubs and related docs.
  - Validates schema and writes to `public/data/top-hub.json`.
  - Falls back to a minimal built-in default if CDN fails (so build never breaks).
- Add UI component (`components/TopHubSignalPanel.tsx`) that:
  - Reads `/data/top-hub.json` (static or client-side fetch depending on framework).
  - Renders hub name, short description, and related doc links.
  - Includes graceful empty/error states.
- Wire into existing dashboard layout (likely `pages/dashboard` or equivalent) with a single import.
- Add npm script: `"fetch:top-hub": "bash scripts/fetch-top-hub.sh"` and integrate into build step (`prebuild` or CI).

### Why this scope
- Zero runtime HF API calls → no 429/rate-limit risk.
- No backend/database changes → deployable quickly.
- Uses CDN bypass pattern from project notes.
- Keeps Costinel “signal-only” behavior (no execution/automation).

---

## File: scripts/fetch-top-hub.sh

```bash
#!/usr/bin/env bash
set -euo pipefail

# Configuration
HF_DATASET_REPO="axentx/costinel-hub-index"   # adjust if different
HF_PATH="top-hub/latest.json"
CDN_URL="https://huggingface.co/datasets/${HF_DATASET_REPO}/resolve/main/${HF_PATH}"
OUT_DIR="public/data"
OUT_FILE="${OUT_DIR}/top-hub.json"
FALLBACK_FILE="scripts/fallback-top-hub.json"

echo "=> Fetching top-hub artifact from CDN..."
mkdir -p "${OUT_DIR}"

if curl -fsSL --retry 3 --retry-delay 2 -o "${OUT_FILE}.tmp" "${CDN_URL}"; then
  # Basic schema validation (must be JSON with top-level "hub" and "related")
  if jq -e '.hub and (.related | type == "array")' "${OUT_FILE}.tmp" > /dev/null 2>&1; then
    mv "${OUT_FILE}.tmp" "${OUT_FILE}"
    echo "✓ CDN fetch successful -> ${OUT_FILE}"
  else
    echo "✗ CDN artifact failed schema validation; using fallback"
    rm -f "${OUT_FILE}.tmp"
    cp "${FALLBACK_FILE}" "${OUT_FILE}"
  fi
else
  echo "✗ CDN fetch failed (network or 404); using fallback"
  cp "${FALLBACK_FILE}" "${OUT_FILE}"
fi

# Ensure valid JSON output
if ! jq empty "${OUT_FILE}" 2>/dev/null; then
  echo "✗ Output invalid JSON; restoring fallback"
  cp "${FALLBACK_FILE}" "${OUT_FILE}"
fi

echo "=> Done."
```

---

## File: scripts/fallback-top-hub.json

```json
{
  "hub": "MOC",
  "title": "Mission Operations Center",
  "description": "Central hub for operational signals and cross-service coordination.",
  "related": [
    {
      "title": "Operational Playbook",
      "url": "/docs/ops-playbook",
      "type": "doc"
    },
    {
      "title": "Cost Signals Taxonomy",
      "url": "/docs/signals-taxonomy",
      "type": "doc"
    },
    {
      "title": "Governance Workflow",
      "url": "/docs/governance-workflow",
      "type": "process"
    }
  ],
  "lastUpdated": "2026-01-01T00:00:00Z"
}
```

---

## File: components/TopHubSignalPanel.tsx

```tsx
'use client';

import { useEffect, useState } from 'react';

type RelatedItem = {
  title: string;
  url: string;
  type: string;
};

type TopHubData = {
  hub: string;
  title: string;
  description: string;
  related: RelatedItem[];
  lastUpdated?: string;
};

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch('/data/top-hub.json', { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error('Failed to load top-hub data');
        return res.json();
      })
      .then((json) => setData(json))
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <p className="text-sm text-muted-foreground">Loading top hub signal…</p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <p className="text-sm text-muted-foreground">Top hub signal unavailable.</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card p-5 shadow-sm">
      <div className="mb-3 flex items-baseline justify-between">
        <h3 className="text-lg font-semibold">{data.hub}</h3>
        <span className="text-xs text-muted-foreground">
          {data.lastUpdated ? new Date(data.lastUpdated).toLocaleDateString() : ''}
        </span>
      </div>
      <p className="mb-4 text-sm text-muted-foreground">{data.description}</p>

      <div>
        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Related signals
        </p>
        <ul className="space-y-1.5">
          {data.related.map((item, idx) => (
            <li key={idx}>
              <a
                href={item.url}
                className="flex items-center gap-2 text-sm text-primary hover:underline"
              >
                <span className="h-1.5 w-1.5 rounded-full bg-primary/60" />
                {item.title}
                <span className="ml-auto text-xs text-muted-foreground">{item.type}</span>
              </a>
            </li>
          ))}
        </ul>
      </div>

      <div className="mt-4 pt-3 border-t border-border text-xs text-muted-foreground">
        Sense + Signal — ไม่ Execute
      </div>
    </div>
  );
}
```

---

## Integration Steps (quick)

1. Add files:
   - `scripts/fetch-top-hub.sh`
   - `scripts/fallback-top-hub.json`
   - `components/TopHubSignalPanel.tsx`

2. Make script executable:
   ```bash
   chmod +x scripts/fetch-top-hub.sh
   ```

3. Add to `package.json` scripts:
   ```json
   "scripts": {
     "fetch:top-hub": "bash scripts/fetch-top-hub.sh",
     "prebuild": "npm run fetch:top-hub",
     "build": "next build"
   }
   ```

4. Import panel into dashboard page (example):
   ```tsx
   // app/dashboard/page.tsx  (or pages/dashboard.tsx)
   import TopHubSignalPanel from '@/components/TopHubSignalPanel';

   export default function DashboardPage() {
     return (
       <div className="grid gap-6">
         <TopHubSignalPanel />
         {/* rest of dashboard */}
       </div>
     );
   }
   ```

5. Test locally:
   ```bash
   npm run fetch:top-hub
   npm run build
   npm run
