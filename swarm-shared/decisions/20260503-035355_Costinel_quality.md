# Costinel / quality

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build time (zero HF API calls at runtime).

---

### 1) Build-time data pipeline (Mac/CI) — 15 min
- Single `list_repo_tree` call for `knowledge-rag/top-hubs/` (non-recursive) → pick latest date folder.
- Fetch `top-hub.json` via CDN (`https://huggingface.co/datasets/.../resolve/main/...`) and embed into repo at build time.
- Output: `/public/signals/top-hub.json` (committed or injected during CI).

**Script** (`scripts/fetch-top-hub.sh`):
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="AXENTX/Knowledge-RAG"
BASE="knowledge-rag/top-hubs"
OUT="public/signals/top-hub.json"

# Get latest date folder (non-recursive)
LATEST=$(curl -s "https://huggingface.co/api/datasets/${REPO}/tree?path=${BASE}&recursive=false" | jq -r '.[].path' | sort -r | head -n1)
if [[ -z "$LATEST" ]]; then
  echo "No hub data found"
  exit 0
fi

# CDN fetch (no auth, bypasses API rate limits)
URL="https://huggingface.co/datasets/${REPO}/resolve/main/${BASE}/${LATEST}/top-hub.json"
mkdir -p "$(dirname "$OUT")"
curl -sSL "$URL" -o "$OUT"
echo "Top-hub baked: $OUT"
```
- Make executable: `chmod +x scripts/fetch-top-hub.sh`
- Run in CI before frontend build; or run locally before `npm run build`.

---

### 2) Frontend signal panel component — 45 min
Create a lightweight, non-blocking panel that hydrates from baked JSON and shows the top hub with context.

**File**: `src/components/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState } from "react";
import { ExternalLink, TrendingUp, Hash } from "lucide-react";

interface HubInsight {
  hub: string;
  connections: number;
  summary: string;
  updated: string; // ISO
  docs: Array<{ title: string; url: string; relevance: number }>;
}

export default function TopHubSignalPanel() {
  const [data, setData] = useState<HubInsight | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // CDN-first baked asset; no HF API calls at runtime
    fetch("/signals/top-hub.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="animate-pulse rounded-lg bg-gray-100 dark:bg-gray-800 p-4 h-32" />
    );
  }

  if (!data) {
    return null; // non-blocking — silently hide if unavailable
  }

  return (
    <aside className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 shadow-sm">
      <div className="flex items-center gap-2 mb-3">
        <TrendingUp className="h-4 w-4 text-amber-600" />
        <span className="text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
          Top Hub Signal
        </span>
      </div>

      <div className="mb-2">
        <div className="flex items-baseline gap-2">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            {data.hub}
          </h3>
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 dark:bg-amber-900/30 px-2 py-0.5 text-xs font-medium text-amber-700 dark:text-amber-300">
            <Hash className="h-3 w-3" />
            {data.connections}
          </span>
        </div>
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
          Updated {new Date(data.updated).toLocaleDateString()}
        </p>
      </div>

      <p className="text-sm text-gray-700 dark:text-gray-300 mb-3 line-clamp-3">
        {data.summary}
      </p>

      <div className="space-y-1.5">
        {data.docs.slice(0, 3).map((doc, i) => (
          <a
            key={i}
            href={doc.url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-between gap-2 text-xs text-blue-600 dark:text-blue-400 hover:underline"
          >
            <span className="truncate">{doc.title}</span>
            <ExternalLink className="h-3 w-3 flex-shrink-0" />
          </a>
        ))}
      </div>
    </aside>
  );
}
```

---

### 3) Integrate into dashboard — 15 min
Add panel to the cost dashboard layout (non-blocking; placed in sidebar or top of a card).

**File**: `src/pages/Dashboard.tsx` (or wherever main dashboard lives)
```tsx
import TopHubSignalPanel from "@/components/TopHubSignalPanel";

export default function Dashboard() {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Main cost panels */}
      <div className="lg:col-span-2 space-y-6">
        {/* existing cost cards */}
      </div>

      {/* Sidebar signals */}
      <aside className="space-y-6">
        <TopHubSignalPanel />
        {/* other signals can go here */}
      </aside>
    </div>
  );
}
```

---

### 4) Styling polish — 10 min
Add minimal line-clamp utility (if not present). In `src/index.css` or globals:
```css
@tailwind base;
@tailwind components;
@tailwind utilities;

.line-clamp-3 {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
```

---

### 5) CI / build integration — 10 min
Ensure baked JSON is produced before frontend build.

**Example** (GitHub Actions or local build script):
```yaml
- name: Fetch Top-Hub Signal
  run: bash scripts/fetch-top-hub.sh

- name: Build frontend
  run: npm run build
```

---

### 6) Acceptance criteria
- Panel appears on dashboard when baked JSON exists.
- No network calls to HuggingFace API at runtime (verify in Network tab).
- Panel gracefully hides if JSON missing or invalid (non-blocking).
- Build script produces valid `public/signals/top-hub.json`.

---

### Estimated effort
- Build script + integration: 15 min
- Component: 45 min
- Layout + styling: 25 min
- CI wiring: 10 min  
**Total**: ~95 min (<2h)
