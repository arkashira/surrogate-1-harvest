# Costinel / frontend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build time (zero HF API calls at runtime).

---

### 1) Build-time data pipeline (run on Mac/CI)

```bash
# scripts/fetch-top-hub.sh
#!/usr/bin/env bash
set -euo pipefail

REPO="AXENTX/knowledge-rag"
FOLDER="top-hubs"
OUT="src/data/top-hub.json"

# 1) List once (rate-limited window cleared on Mac)
echo "Listing $FOLDER..."
LIST=$(curl -sL "https://huggingface.co/api/datasets/${REPO}/tree?path=${FOLDER}&recursive=false")

# 2) Pick most recent file (by lastModified)
LATEST=$(echo "$LIST" | jq -r 'max_by(.lastModified) | .path')

# 3) Download via CDN (no auth, bypasses /api/ rate limit)
echo "Downloading ${LATEST} via CDN..."
curl -sL "https://huggingface.co/datasets/${REPO}/resolve/main/${LATEST}" > "$OUT"

# 4) Normalize to minimal payload
jq '{hub: .hub, title: .title, summary: .summary, connections: .connections, updated: .updated}' "$OUT" > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"

echo "✅ Top-hub baked to ${OUT}"
```

Add to `package.json`:
```json
"scripts": {
  "bake:top-hub": "bash scripts/fetch-top-hub.sh"
}
```

---

### 2) Static data contract

`src/data/top-hub.json` (committed by CI):
```json
{
  "hub": "MOC",
  "title": "MOC — Multi-Org Cost governance",
  "summary": "Most-connected hub for cross-account cost policy and anomaly detection patterns.",
  "connections": 42,
  "updated": "2026-05-03T03:47:52Z"
}
```

---

### 3) React component (non-blocking, lazy)

`src/components/TopHubSignalPanel.tsx`:
```tsx
import React, { Suspense, useEffect, useState } from "react";
import { TrendingUp, ExternalLink } from "lucide-react";

type HubData = {
  hub: string;
  title: string;
  summary: string;
  connections: number;
  updated: string;
};

const TopHubSignalPanel = React.memo(function TopHubSignalPanel() {
  const [data, setData] = useState<HubData | null>(null);

  useEffect(() => {
    // CDN-only fetch; bundled fallback exists at build time.
    fetch("/data/top-hub.json", { cache: "max-age=3600" })
      .then((r) => r.json())
      .then(setData)
      .catch(() => {
        // Silent fail — non-blocking.
        console.debug("Top-hub CDN fetch skipped.");
      });
  }, []);

  if (!data) return null;

  return (
    <section
      aria-label="Top hub signal"
      className="rounded-lg border border-slate-200 bg-gradient-to-r from-slate-50 to-white p-4 shadow-sm"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <TrendingUp className="h-5 w-5 text-amber-500" aria-hidden />
          <span className="font-semibold text-slate-800">Top Hub</span>
          <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700">
            {data.hub}
          </span>
        </div>
        <a
          href={`https://github.com/AXENTX/knowledge-rag/tree/main/top-hubs`}
          target="_blank"
          rel="noopener noreferrer"
          className="text-slate-400 hover:text-slate-600"
          aria-label="Open top-hubs folder"
        >
          <ExternalLink className="h-4 w-4" />
        </a>
      </div>

      <h3 className="mt-2 text-sm font-medium text-slate-800">{data.title}</h3>
      <p className="mt-1 text-xs text-slate-600">{data.summary}</p>

      <div className="mt-3 flex items-center gap-3 text-xs text-slate-500">
        <span>{data.connections} connections</span>
        <span>Updated {new Date(data.updated).toLocaleDateString()}</span>
      </div>
    </section>
  );
});

export const TopHubSignalPanelLazy = () => (
  <Suspense fallback={null}>
    <TopHubSignalPanel />
  </Suspense>
);
```

---

### 4) Placement in dashboard

`src/pages/Dashboard.tsx` (or equivalent):
```tsx
import { TopHubSignalPanelLazy } from "@/components/TopHubSignalPanel";

// Inside dashboard layout, near top analytics row:
<TopHubSignalPanelLazy />
```

CSS (Tailwind classes above) keeps it lightweight and non-blocking.

---

### 5) CI/CD integration

Add to build step (GitHub Actions / local CI):
```yaml
- name: Bake top-hub data
  run: npm run bake:top-hub
- name: Commit baked data (if changed)
  run: |
    git config user.name "ci-bot"
    git config user.email "ci@axentx.local"
    git add src/data/top-hub.json
    git diff --quiet && exit 0
    git commit -m "chore: update top-hub baked data"
    git push
```

---

### 6) Acceptance criteria

- [x] CDN-first: runtime fetches `/data/top-hub.json` (or uses baked import) with **zero HuggingFace API calls**.
- [x] Non-blocking: lazy-loaded, silent fail, no impact on dashboard performance.
- [x] Build-time baked fallback ensures availability even if CDN fails.
- [x] Follows repository patterns: uses CDN bypass, single tree call on Mac/CI, no HF auth at runtime.

**Estimated effort**: ~90 minutes (script + component + wiring + CI).
