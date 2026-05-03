# Costinel / backend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") with **zero HuggingFace API calls at runtime**. Data is baked at build time and fetched via CDN (no Authorization header, bypasses API rate limits).

---

### Architecture (CDN-first)

1. **Build-time (Mac orchestration)**  
   - Single `list_repo_tree` call to `knowledge-rag/hubs/` (non-recursive) for today’s folder.  
   - Save `hubs-index.json` locally.  
   - Compute top-hub by degree (most connections).  
   - Produce `top-hub.json` (minimal payload: `{slug, title, degree, summary, updatedAt}`).  
   - Upload `top-hub.json` to `costinel-data/top-hub/{YYYYMMDD}.json` via CDN (`/resolve/main/...`).  

2. **Runtime (Costinel frontend)**  
   - Fetch `https://huggingface.co/datasets/costinel-data/resolve/main/top-hub/{YYYYMMDD}.json` (no auth, CDN tier).  
   - Cache in `localStorage` with 10-minute TTL.  
   - Render a non-blocking signal panel in the dashboard sidebar.  
   - If CDN fetch fails → graceful fallback (cached stale data or empty).  

3. **Lightning training / ingestion**  
   - No changes required; this panel is purely frontend signal.  
   - If future training needs hub graph, reuse same CDN file list strategy (zero API during training).  

---

### Implementation Steps (≤2h)

#### 1) Add build script (mac orchestration)  
Create `/opt/axentx/Costinel/scripts/build-top-hub.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# Config
REPO="costinel-data"
DATE=$(date -u +%Y%m%d)
OUT_DIR="public/data/top-hub"
OUT_FILE="${OUT_DIR}/${DATE}.json"
HF_USER="axentx"  # or your org

mkdir -p "$(dirname "$OUT_FILE")"

# 1) List today's folder (non-recursive)
# Uses huggingface_hub CLI; ensure token is set in env for write (read is public)
echo "Listing hubs index for ${DATE}..."
hf repo tree ${HF_USER}/${REPO}/hubs/${DATE} --recursive false --json > /tmp/hubs_tree.json || {
  echo "No folder for ${DATE}, creating empty index"
  echo '[]' > /tmp/hubs_tree.json
}

# 2) Compute top-hub (simplified: pick first by name if no degree metadata)
# In real usage, join with graph degree from knowledge-rag output
TOP_HUB=$(jq -r '
  if length == 0 then
    {slug: "MOC", title: "MOC", degree: 0, summary: "Default hub", updatedAt: now | todate}
  else
    .[0] | {slug: .path, title: (.path | split(".")[0]), degree: 1, summary: "Active hub", updatedAt: now | todate}
  end
' /tmp/hubs_tree.json)

echo "$TOP_HUB" | jq . > "$OUT_FILE"
echo "Built ${OUT_FILE}"

# 3) Upload to HF dataset repo (optional: only if you want to publish)
# Uses CDN path; upload via git or huggingface_hub CLI
if command -v huggingface_hub >/dev/null 2>&1; then
  huggingface_hub upload \
    --repo-type dataset \
    "${HF_USER}/${REPO}" \
    "$OUT_FILE" \
    "top-hub/${DATE}.json" \
    --token "${HF_TOKEN:-}" || echo "Upload skipped (no token)"
else
  echo "huggingface_hub CLI not found — skipping upload"
fi
```

Make executable:

```bash
chmod +x /opt/axentx/Costinel/scripts/build-top-hub.sh
```

---

#### 2) Add frontend signal panel component  
Create `/opt/axentx/Costinel/src/components/TopHubSignalPanel.tsx`

```tsx
import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ExternalLink } from "lucide-react";

interface TopHub {
  slug: string;
  title: string;
  degree: number;
  summary: string;
  updatedAt: string;
}

const CDN_BASE = "https://huggingface.co/datasets/costinel-data/resolve/main";
const CACHE_KEY = "costinel:top-hub";
const CACHE_TTL_MS = 10 * 60 * 1000; // 10m

function getCached(): TopHub | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const { data, ts } = JSON.parse(raw);
    if (Date.now() - ts > CACHE_TTL_MS) return null;
    return data as TopHub;
  } catch {
    return null;
  }
}

function setCached(data: TopHub) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({ data, ts: Date.now() }));
  } catch {
    // ignore
  }
}

export function TopHubSignalPanel() {
  const [hub, setHub] = useState<TopHub | null>(getCached());
  const [loading, setLoading] = useState(!hub);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (hub) {
      // still try refresh in background
      fetchFresh();
      return;
    }
    fetchFresh();
  }, []);

  async function fetchFresh() {
    try {
      setLoading(true);
      setError(false);
      const today = new Date().toISOString().slice(0, 10).replace(/-/g, "");
      const url = `${CDN_BASE}/top-hub/${today}.json`;
      const res = await fetch(url, { cache: "no-store" });
      if (!res.ok) throw new Error("CDN fetch failed");
      const data = (await res.json()) as TopHub;
      setHub(data);
      setCached(data);
    } catch {
      setError(true);
      // keep stale cache if available
    } finally {
      setLoading(false);
    }
  }

  if (loading && !hub) {
    return (
      <Card className="w-full">
        <CardHeader>
          <Skeleton className="h-5 w-32" />
        </CardHeader>
        <CardContent>
          <Skeleton className="h-4 w-full mb-2" />
          <Skeleton className="h-4 w-3/4" />
        </CardContent>
      </Card>
    );
  }

  if (!hub) {
    return null; // silent fallback
  }

  return (
    <Card className="w-full">
      <CardHeader className="pb-3">
        <CardTitle className="text-sm font-medium flex items-center justify-between">
          Top Hub Signal
          <span className="text-xs text-muted-foreground font-normal">
            {new Date(hub.updatedAt).toLocaleDateString()}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="font-semibold">{hub.title}</span>
            <span className="text-xs text-muted-foreground">({hub.slug})</span>
          </div>
          <p className="text-sm text-muted-foreground">{hub.summary}</p>
          <div className="flex items-center gap-1 text-xs text-muted-foreground">
            <span>Connections: {hub.degree}</span>
          </div>
          <a
            href={`https://huggingface.co/datasets/costinel-data/tree/main/hubs`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-blue
