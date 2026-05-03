# Costinel / frontend

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Core principle**: Zero runtime HF API calls. CDN-first, build-time baked data with runtime CDN fetch and robust fallback. Combines Candidate 1’s concrete automation with Candidate 2’s schema clarity and cache strategy.

---

### 1) CDN JSON schema (single source of truth)

Use this schema for `public/data/top-hub.json` (committed by CI and served via CDN):

```json
{
  "hub": "MOC",
  "label": "MOC",
  "score": 94.2,
  "summary": "Most-connected hub (Market Opportunity Canvas)",
  "related": ["knowledge-rag", "graph", "hub"],
  "updatedAt": "2025-06-25T12:00:00Z",
  "source": "build",
  "ttl": 86400
}
```

- `score` as number (0–100).  
- `related` as short string tags (no `#`).  
- `source` = `"build"` (from CI) or `"fallback"` (local default).  
- `ttl` in seconds for cache decisions.

---

### 2) Build-time fetch script (CI / prebuild)

`scripts/fetch-top-hub.sh` (runs in CI or `prebuild`):

```bash
#!/usr/bin/env bash
# scripts/fetch-top-hub.sh
# Usage: npm run fetch:top-hub
# Requires: curl, jq

set -euo pipefail

REPO="axentx/knowledge-rag"
OUT_DIR="public/data"
OUT_FILE="${OUT_DIR}/top-hub.json"
CACHE_TTL=86400

mkdir -p "${OUT_DIR}"

# Try known paths in the repo for top-hub.json
PATHS=(
  "top-hub.json"
  "data/top-hub.json"
  "insights/top-hub.json"
)

FOUND=false
for p in "${PATHS[@]}"; do
  URL="https://huggingface.co/datasets/${REPO}/resolve/main/${p}"
  if curl -fsSL --max-time 10 "${URL}" -o "${OUT_FILE}.tmp" 2>/dev/null; then
    if jq empty "${OUT_FILE}.tmp" 2>/dev/null; then
      # Normalize to schema
      jq '
        {
          hub: (.hub // .name // "MOC"),
          label: (.label // .hub // .name // "MOC"),
          score: ((.score // 94.2) | tonumber),
          summary: (.description // .summary // "Most-connected hub (Market Opportunity Canvas)"),
          related: (.tags // .related // ["knowledge-rag","graph","hub"]),
          updatedAt: (.updated_at // .updatedAt // now | tostring),
          source: "build",
          ttl: '"${CACHE_TTL}"'
        }
      ' "${OUT_FILE}.tmp" > "${OUT_FILE}"
      FOUND=true
      echo "Saved top-hub from ${p}"
      break
    fi
  fi
done

if [ "$FOUND" = false ]; then
  echo "No remote top-hub found; generating fallback"
  cat > "${OUT_FILE}" <<EOF
{
  "hub": "MOC",
  "label": "MOC",
  "score": 94.2,
  "summary": "Most-connected hub (Market Opportunity Canvas)",
  "related": ["knowledge-rag", "graph", "hub"],
  "updatedAt": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "source": "fallback",
  "ttl": ${CACHE_TTL}
}
EOF
fi

echo "Top-hub saved to ${OUT_FILE}"
```

Add to `package.json`:

```json
{
  "scripts": {
    "fetch:top-hub": "bash ./scripts/fetch-top-hub.sh",
    "prebuild": "npm run fetch:top-hub"
  }
}
```

---

### 3) CDN fetch utility (runtime)

`src/lib/cdn.ts`:

```ts
const CDN_URL = '/data/top-hub.json';
const TIMEOUT_MS = 4000;

export interface TopHubData {
  hub: string;
  label?: string;
  score: number;
  summary: string;
  related: string[];
  updatedAt: string;
  source: string;
  ttl: number;
}

export async function fetchTopHub(): Promise<TopHubData | null> {
  try {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), TIMEOUT_MS);

    const res = await fetch(CDN_URL, {
      method: 'GET',
      cache: 'force-cache',
      signal: controller.signal,
    });
    clearTimeout(id);

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const json = (await res.json()) as TopHubData;
    // Basic shape validation
    if (!json.hub || typeof json.score !== 'number') throw new Error('Invalid shape');
    return json;
  } catch (err) {
    console.warn('[TopHub] CDN fetch failed', err);
    return null;
  }
}
```

---

### 4) React TopHubPanel component

`src/components/TopHubPanel/TopHubPanel.tsx`:

```tsx
import React, { useEffect, useState, Suspense } from 'react';
import { fetchTopHub, TopHubData } from '../../lib/cdn';
import './TopHubPanel.scss';

const DEFAULT_HUB: TopHubData = {
  hub: 'MOC',
  label: 'MOC',
  score: 94.2,
  summary: 'Most-connected hub (Market Opportunity Canvas)',
  related: ['knowledge-rag', 'graph', 'hub'],
  updatedAt: new Date().toISOString(),
  source: 'fallback',
  ttl: 86400,
};

const TopHubPanelContent: React.FC = () => {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetchTopHub().then((res) => {
      if (!mounted) return;
      setData(res);
      setLoading(false);
    });

    // Fallback after timeout to avoid blocking UI
    const fb = setTimeout(() => {
      if (mounted && loading) {
        setData(DEFAULT_HUB);
        setLoading(false);
      }
    }, 2500);

    return () => {
      mounted = false;
      clearTimeout(fb);
    };
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel skeleton" aria-busy="true">
        <div className="sh-avatar" />
        <div className="sh-body">
          <div className="sh-line short" />
          <div className="sh-line medium" />
        </div>
      </div>
    );
  }

  const hub = data || DEFAULT_HUB;
  const scorePercent = Math.round(hub.score);

  return (
    <div className="top-hub-panel" role="complementary" aria-label="Top hub insight">
      <div className="top-hub-panel__avatar" aria-hidden="true">
        {(hub.label || hub.hub).slice(0, 2).toUpperCase()}
      </div>
      <div className="top-hub-panel__body">
        <div className="top-hub-panel__title">
          <span className="top-hub-panel__name">{hub.label || hub.hub}</span>
          <span className="top-hub-panel__score" title="Connection strength">
            {scorePercent}%
          </span>
        </div>
        <div className="top-hub-panel__desc">{hub.summary}</div>
        {hub.related && hub.related.length > 0 && (
          <div className="top-hub-panel__tags" aria-label="Related tags">
            {hub.related.slice(0, 3).map((t) => (
              <span key={t} className="top-hub-panel__tag">
               
