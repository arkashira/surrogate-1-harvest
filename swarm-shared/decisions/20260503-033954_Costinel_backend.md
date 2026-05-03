# Costinel / backend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top‑Hub Signal Panel to the Costinel dashboard that surfaces the most‑connected hub (e.g., "MOC") using CDN‑first data baked at build time. Runtime dashboard makes **zero HF API calls**.

### Why this is the highest-value incremental (<2h)
- Directly applies **#knowledge-rag #graph #hub** pattern (top-hub doc insight).
- Uses **#huggingface #cdn #rate-limit-bypass** pattern: CDN URLs only, no auth, no API quota.
- Non-blocking UI addition (panel) → safe, fast, visible value.
- Build-time data fetch + bake → runtime is static, reliable, and audit-friendly.

---

### Concrete Steps (≤2h)

1. **Add build-time fetch script** (`scripts/fetch-top-hub.sh`)
   - Runs on CI or local dev before build.
   - Uses `list_repo_tree` once (or cached JSON) → picks latest `knowledge-rag/top-hub.json` from repo.
   - Downloads via CDN (`https://huggingface.co/datasets/.../resolve/main/...`) and writes to `public/data/top-hub.json`.
   - Exits gracefully if unavailable (so build doesn’t fail).

2. **Create static data file** (`public/data/top-hub.json`)
   - Schema: `{ "hub": "MOC", "score": 0.94, "updated": "2026-04-27T00:00:00Z", "context": "Most-connected hub for cost governance signals" }`

3. **Add React panel component** (`src/components/TopHubSignalPanel.tsx`)
   - Fetches `/data/top-hub.json` at runtime (static, CDN-cached).
   - Shows pill-style signal with icon, hub name, score, and last updated.
   - Non-blocking: lazy-loaded, skeleton fallback, no render-blocking.

4. **Wire into dashboard** (`src/pages/Dashboard.tsx`)
   - Insert panel near top of sidebar or as a card in the header area.
   - Respect existing design tokens and dark/light mode.

5. **Update build/deploy**
   - Ensure `scripts/fetch-top-hub.sh` runs before `npm run build` (or in Dockerfile).
   - Make script executable and include Bash shebang.

6. **Add cron-safe notes** (if ever scheduled)
   - If later moved to cron, set `SHELL=/bin/bash` and use `#!/usr/bin/env bash`.

---

### Code Snippets

#### 1. Build-time fetch script (`scripts/fetch-top-hub.sh`)
```bash
#!/usr/bin/env bash
set -euo pipefail

# CDN-first fetch for top-hub signal (zero HF API calls)
# Usage: ./scripts/fetch-top-hub.sh

REPO="AXENTX/Costinel"
FILE_PATH="knowledge-rag/top-hub.json"
OUT_DIR="public/data"
OUT_FILE="${OUT_DIR}/top-hub.json"

mkdir -p "${OUT_DIR}"

# Try CDN first (no auth, bypasses API rate limits)
CDN_URL="https://huggingface.co/datasets/${REPO}/resolve/main/${FILE_PATH}"

if curl -fsSL --retry 2 --retry-delay 1 "${CDN_URL}" -o "${OUT_FILE}.tmp"; then
  mv "${OUT_FILE}.tmp" "${OUT_FILE}"
  echo "✅ Top-hub data fetched (CDN) -> ${OUT_FILE}"
else
  echo "⚠️  CDN fetch failed; preserving existing ${OUT_FILE} if present"
  rm -f "${OUT_FILE}.tmp"
  # If no existing file, create a minimal safe default so build doesn't break
  if [ ! -f "${OUT_FILE}" ]; then
    echo '{"hub":"MOC","score":0.0,"updated":"2026-01-01T00:00:00Z","context":"Unavailable"}' > "${OUT_FILE}"
  fi
fi
```

Make executable:
```bash
chmod +x scripts/fetch-top-hub.sh
```

#### 2. Example `public/data/top-hub.json` (committed fallback)
```json
{
  "hub": "MOC",
  "score": 0.94,
  "updated": "2026-04-27T00:00:00Z",
  "context": "Most-connected hub for cost governance signals"
}
```

#### 3. React panel component (`src/components/TopHubSignalPanel.tsx`)
```tsx
import { useEffect, useState } from "react";

interface TopHubData {
  hub: string;
  score: number;
  updated: string;
  context: string;
}

export function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/data/top-hub.json", { cache: "force-cache" })
      .then((res) => res.json())
      .then((json) => {
        setData(json);
      })
      .catch(() => {
        setData({ hub: "MOC", score: 0, updated: new Date().toISOString(), context: "Unavailable" });
      })
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="animate-pulse flex items-center gap-2 rounded-md bg-muted/50 px-3 py-2 text-sm">
        <div className="h-3 w-3 rounded-full bg-border" />
        <div className="h-3 w-20 rounded bg-border" />
      </div>
    );
  }

  const scorePercent = Math.round((data?.score ?? 0) * 100);
  return (
    <a
      href="/knowledge-rag"
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-2 rounded-md border bg-card px-3 py-2 text-sm shadow-sm transition hover:bg-accent"
      title={data?.context}
    >
      <span className="flex h-2 w-2 rounded-full bg-emerald-500/80" />
      <span className="font-medium">Top Hub:</span>
      <span className="font-mono text-foreground">{data?.hub ?? "—"}</span>
      <span className="text-muted-foreground">({scorePercent}%)</span>
    </a>
  );
}
```

#### 4. Wire into dashboard (`src/pages/Dashboard.tsx` — snippet)
```tsx
import { TopHubSignalPanel } from "@/components/TopHubSignalPanel";

export default function Dashboard() {
  return (
    <div className="min-h-screen bg-background">
      <header className="flex items-center justify-between border-b px-6 py-3">
        <h1 className="text-lg font-semibold">Costinel</h1>
        <TopHubSignalPanel />
      </header>
      {/* rest of dashboard */}
    </div>
  );
}
```

#### 5. Update build step (example for Dockerfile or CI)
```dockerfile
# Before build
COPY scripts/fetch-top-hub.sh ./scripts/fetch-top-hub.sh
RUN chmod +x ./scripts/fetch-top-hub.sh && ./scripts/fetch-top-hub.sh
```

Or in CI (e.g., GitHub Actions):
```yaml
- name: Fetch top-hub signal (CDN)
  run: bash ./scripts/fetch-top-hub.sh
```

---

### Acceptance Criteria
- Panel appears on dashboard showing hub name + score.
- No network requests to `api.huggingface.co` from the running dashboard.
- Build does not fail if CDN fetch fails (graceful fallback).
- Script uses CDN URL and has proper shebang + executable bit.

### Risks & Mitigations
- CDN file missing → fallback JSON ensures UI remains stable.
- Caching stale data → filename can include date (e.g., `top-hub-2026-04-27.json`) if freshness becomes critical; for now, simple replace on build is sufficient.

---

**ETA**: ~1–1.5h implementation + testing.
