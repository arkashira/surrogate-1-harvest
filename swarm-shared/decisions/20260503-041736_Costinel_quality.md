# Costinel / quality

**Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)**

---

### Goal
Add a lightweight, resilient “Top‑Hub Signal” panel to Costinel that surfaces the most‑connected hub (e.g., **MOC**) with **zero runtime HF API calls**, using CDN‑first baked data and robust fallbacks.  
Follows Costinel philosophy: *Sense + Signal — no execution/mutations*.

---

### Why (patterns)
- **Top‑hub doc insight** — review most‑connected hub before planning.  
- **HF CDN bypass** — avoid runtime API/rate limits by baking data and serving via CDN/static assets.  
- **Minimal surface** — one build‑time JSON, one fetch hook, one React panel.  
- **Safe & read‑only** — no mutations, no runtime external calls.

---

### Scope (what we ship)
1. **Bake step** (run once on Mac/CI, not in production):  
   - List top‑level of `knowledge-rag` repo for the latest date folder (non‑recursive).  
   - Compute top hub by degree/connections.  
   - Save minimal JSON to `public/data/top-hub.json` (committed or uploaded to CDN).  

2. **Production panel** (frontend):  
   - `src/hooks/useTopHubSignal.js` — CDN‑first fetch with robust fallback chain and caching.  
   - `src/components/TopHubSignalPanel.jsx` — renders accessible card with hub name, score, insight, and related docs.  
   - No runtime HF API calls.

3. **Ops/tooling** (optional):  
   - Small script `scripts/bake-top-hub.sh` (bash) or `scripts/bake-top-hub.js` (Node) for CI/cron.

---

### Implementation Steps (minute-by-minute)

#### 0–15m — Create bake script (CDN‑first, safe)
Use `curl` + HF CDN (no auth required) or HF API in CI. Prefer CDN for rate‑limit safety.

**`scripts/bake-top-hub.sh`**
```bash
#!/usr/bin/env bash
# scripts/bake-top-hub.sh
# Usage: bash scripts/bake-top-hub.sh <dateFolder>
set -euo pipefail

REPO="AXENTX/knowledge-rag"
DATEFOLDER="${1:-$(date +%Y-%m-%d)}"
OUT="public/data/top-hub.json"

mkdir -p "$(dirname "$OUT")"

# Try CDN _index.json first (public, high rate limit)
URL="https://huggingface.co/datasets/${REPO}/resolve/main/${DATEFOLDER}/_index.json"
if curl -fsSL "$URL" -o /tmp/index.json 2>/dev/null; then
  TOP_NAME=$(jq -r '.hubs[0].name // "MOC"' /tmp/index.json 2>/dev/null || echo "MOC")
  TOP_DEGREE=$(jq -r '.hubs[0].degree // 0' /tmp/index.json 2>/dev/null || echo 0)
  TOP_DOCS=$(jq -c '.hubs[0].docs // []' /tmp/index.json 2>/dev/null || echo '[]')
else
  # Fallback: minimal default (can be enhanced with repo tree if needed)
  TOP_NAME="MOC"
  TOP_DEGREE=0
  TOP_DOCS='[]'
fi

cat > "$OUT" <<EOF
{
  "hub": "${TOP_NAME}",
  "score": ${TOP_DEGREE},
  "insight": "Most-connected hub for ${DATEFOLDER} — prioritize for contextual reviews.",
  "relatedDocs": ${TOP_DOCS},
  "generatedAt": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "date": "${DATEFOLDER}",
  "source": "cdn-baked"
}
EOF

echo "Baked top-hub to $OUT"
```
Make executable:
```bash
chmod +x scripts/bake-top-hub.sh
```

If using Node in CI, equivalent with `@huggingface/hub` and `listRepoTree` is acceptable — but keep output format identical.

---

#### 15–40m — Add fetch hook with robust fallback
**`src/hooks/useTopHubSignal.js`**
```js
// CDN-first fetch with robust fallback chain and short cache
const CDN_URL = '/data/top-hub.json';
const CACHE_TTL = 30 * 1000; // 30s
const FALLBACK = { hub: 'MOC', score: 0, insight: 'Default hub — run bake script to generate latest top-hub signal.', relatedDocs: [] };

export default function useTopHubSignal() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let mounted = true;
    let cache = null;
    let cacheTime = 0;

    const fetchData = async () => {
      try {
        // 1) Memory cache (30s)
        if (cache && Date.now() - cacheTime < CACHE_TTL) {
          if (mounted) {
            setData(cache);
            setLoading(false);
            setError(null);
          }
          return;
        }

        // 2) Try CDN JSON
        const res = await fetch(CDN_URL, { cache: 'no-store' });
        if (res.ok) {
          const json = await res.json();
          // Normalize to expected shape
          const normalized = {
            hub: json.hub || json.topHub?.name || FALLBACK.hub,
            score: Number(json.score || json.topHub?.degree || 0),
            insight: json.insight || json.topHub?.insight || FALLBACK.insight,
            relatedDocs: Array.isArray(json.relatedDocs || json.topHub?.docs)
              ? (json.relatedDocs || json.topHub?.docs)
              : FALLBACK.relatedDocs
          };
          cache = normalized;
          cacheTime = Date.now();
          if (mounted) {
            setData(normalized);
            setError(null);
          }
          return;
        }
        throw new Error(`CDN fetch failed: ${res.status}`);
      } catch (err) {
        // 3) Try inlined SSR/static data
        if (typeof window !== 'undefined' && window.__TOP_HUB__) {
          try {
            const inlined = window.__TOP_HUB__;
            const normalized = {
              hub: inlined.hub || inlined.topHub?.name || FALLBACK.hub,
              score: Number(inlined.score || inlined.topHub?.degree || 0),
              insight: inlined.insight || inlined.topHub?.insight || FALLBACK.insight,
              relatedDocs: Array.isArray(inlined.relatedDocs || inlined.topHub?.docs)
                ? (inlined.relatedDocs || inlined.topHub?.docs)
                : FALLBACK.relatedDocs
            };
            if (mounted) {
              setData(normalized);
              setError(null);
            }
            return;
          } catch {
            // fall through
          }
        }

        // 4) Safe default
        if (mounted) {
          setData(FALLBACK);
          setError(err instanceof Error ? err.message : 'Unknown error');
        }
      } finally {
        if (mounted) setLoading(false);
      }
    };

    fetchData();

    // Optional background revalidation every 5m
    const interval = setInterval(fetchData, 5 * 60 * 1000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  return { data, loading, error };
}
```

---

#### 40–80m — Add accessible panel component
**`src/components/TopHubSignalPanel.jsx`**
```jsx
import React from 'react';
import useTopHubSignal from '../hooks/useTopHubSignal';
import './TopHubSignalPanel.css';

export default function TopHubSignalPanel() {
  const { data, loading, error } = useTopHubSignal();
  const hub = data || { hub: 'MOC', score: 0, insight: 'Loading…', relatedDocs: [] };

  return (
    <aside className="top-hub-signal-panel" aria-label
