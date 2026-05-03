# Costinel / backend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") with **zero HuggingFace API calls at runtime**. Data is baked at build time (or cron) and served via CDN; frontend fetches static JSON from public CDN URL.

---

### Architecture (CDN-first)
1. **Mac orchestration script** (`scripts/update-top-hub.sh`)
   - Runs on schedule (cron) or manually after `knowledge-rag` runs.
   - Calls `list_repo_tree` once (per date folder) → saves `top-hub.json`.
   - Commits/pushes to `data/top-hub/YYYY-MM-DD.json` (or uploads to CDN/public path).
2. **Static CDN path**  
   `https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/data/top-hub/latest.json`
   - Public, no auth, bypasses API rate limits.
3. **Frontend panel** (`components/TopHubSignalPanel.tsx`)
   - Fetches CDN JSON client-side (or at build via ISR/SSG if Next.js).
   - Shows hub name, short summary, link to related docs.
   - Graceful fallback if CDN unavailable.

---

### Implementation Steps (≤2h)

#### 1) Add backend helper script (Mac orchestration)
Create `scripts/update-top-hub.sh` (executable, Bash shebang).

```bash
#!/usr/bin/env bash
# scripts/update-top-hub.sh
# Usage: bash scripts/update-top-hub.sh
# Expects HF_TOKEN in env for write (or use local git push).
set -euo pipefail

REPO="AXENTX/Costinel"
OUT_DIR="data/top-hub"
DATE=$(date +%Y-%m-%d)
OUT_FILE="${OUT_DIR}/${DATE}.json"
LATEST_FILE="${OUT_DIR}/latest.json"

# Ensure output dir exists
mkdir -p "${OUT_DIR}"

# 1) Query knowledge-rag or local graph to find top hub (stub: MOC)
# Replace this block with actual call to your knowledge-rag/top-hub extraction.
# For now, produce a minimal payload matching expected shape.
cat > "${OUT_FILE}" <<EOF
{
  "hub": "MOC",
  "label": "Mission Operations Center",
  "score": 0.94,
  "summary": "Central hub for mission ops, on-call, and incident response playbooks.",
  "related_docs": [
    {"title": "On-call rotation", "path": "docs/ops/oncall.md"},
    {"title": "Incident commander", "path": "docs/ops/ic.md"}
  ],
  "updated_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "source": "knowledge-rag"
}
EOF

# 2) Copy to latest.json (for CDN stable path)
cp "${OUT_FILE}" "${LATEST_FILE}"

# 3) Commit and push (or use huggingface_hub to upload)
# If repo is checked out locally and remote is writable:
git add "${OUT_FILE}" "${LATEST_FILE}"
git commit -m "chore: update top-hub ${DATE}" || true
git push origin main

# If using huggingface_hub CLI (alternative):
# huggingface_hub upload-file \
#   --repo-type dataset --repo-id "${REPO}" \
#   "${LATEST_FILE}" "data/top-hub/latest.json"

echo "✅ Updated top-hub: ${DATE}"
```

Make executable:
```bash
chmod +x scripts/update-top-hub.sh
```

Cron (if desired) — ensure `SHELL=/bin/bash`:
```cron
SHELL=/bin/bash
0 6 * * * cd /opt/axentx/Costinel && bash scripts/update-top-hub.sh >> /var/log/costinel-top-hub.log 2>&1
```

---

#### 2) Add frontend panel component
Create `components/TopHubSignalPanel.tsx` (adjust framework as needed).

```tsx
// components/TopHubSignalPanel.tsx
'use client';

import { useEffect, useState } from 'react';

type RelatedDoc = { title: string; path: string };
type TopHubPayload = {
  hub: string;
  label: string;
  score: number;
  summary: string;
  related_docs: RelatedDoc[];
  updated_at: string;
  source: string;
};

const CDN_JSON =
  'https://huggingface.co/datasets/AXENTX/Costinel/resolve/main/data/top-hub/latest.json';

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function fetchTopHub() {
      try {
        const res = await fetch(CDN_JSON, { cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = (await res.json()) as TopHubPayload;
        if (!cancelled) {
          setData(json);
          setError(null);
        }
      } catch (err: any) {
        if (!cancelled) setError(err.message || 'Failed to load top hub');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchTopHub();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return (
      <div className="rounded border border-gray-200 bg-gray-50 p-4">
        <p className="text-sm text-gray-500">Loading top hub signal…</p>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="rounded border border-yellow-100 bg-yellow-50 p-4">
        <p className="text-sm text-yellow-800">Signal unavailable.</p>
      </div>
    );
  }

  return (
    <div className="rounded border border-blue-200 bg-blue-50 p-4">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-sm font-semibold text-blue-900">Top Hub Signal</h3>
          <p className="text-lg font-medium text-blue-800">{data.hub}</p>
          <p className="text-sm text-blue-700">{data.label}</p>
          <p className="mt-2 text-sm text-blue-800">{data.summary}</p>

          {data.related_docs.length > 0 && (
            <ul className="mt-3 space-y-1">
              {data.related_docs.map((doc, idx) => (
                <li key={idx}>
                  <a
                    href={`/${doc.path}`}
                    className="text-sm text-blue-600 underline hover:text-blue-800"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {doc.title}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>
        <span className="text-xs text-blue-500">{data.updated_at.split('T')[0]}</span>
      </div>
    </div>
  );
}
```

---

#### 3) Integrate into dashboard
Add panel to an existing dashboard page (example for a Next.js page):

```tsx
// app/dashboard/page.tsx (or pages/dashboard.tsx)
import TopHubSignalPanel from '@/components/TopHubSignalPanel';

export default function DashboardPage() {
  return (
    <main className="p-6">
      <h1 className="mb-4 text-2xl font-bold">Cost Dashboard</h1>

      {/* Top-hub signal (non-blocking) */}
      <div className="mb-6 max-w-2xl">
        <TopHubSignalPanel />
      </div>

      {/* Existing cost panels below */}
      {/* ... */}
    </main>
  );
}

