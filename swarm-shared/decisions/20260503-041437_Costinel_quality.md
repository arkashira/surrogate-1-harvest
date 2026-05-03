# Costinel / quality

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a lightweight, resilient “Top-Hub Signal” panel to Costinel that surfaces the most‑connected hub (e.g., “MOC”) with **zero runtime HF API calls**, using CDN‑first baked data and robust fallbacks.

### Scope (what we ship)
- New panel component: `TopHubSignalPanel`
- Build-time / deploy-time data fetch (Mac orchestration) that writes `public/data/top-hub.json`
- Runtime panel reads `public/data/top-hub.json` (CDN path) — no auth, no API quota
- Graceful fallback UI when data missing or stale (>7d)
- Small inline docs and a cron-safe wrapper for data refresh

### Why this is highest value (<2h)
- Directly applies “top-hub doc insight” and “HF CDN bypass” patterns.
- Zero runtime cost/rate-limit risk.
- Improves governance context for cloud cost decisions (Sense + Signal).
- Small surface area: one JSON, one component, one script.

---

## 1. Data pipeline (Mac orchestration only)

Script: `scripts/fetch-top-hub.sh`

```bash
#!/usr/bin/env bash
# scripts/fetch-top-hub.sh
# Run on Mac (or CI) — never in production runtime.
# Uses HF API once per refresh to list tree and CDN to download the top-hub artifact.
# Produces: public/data/top-hub.json

set -euo pipefail
export SHELL=/bin/bash

REPO="AXENTX/KnowledgeGraph"
OUT_DIR="public/data"
OUT_FILE="${OUT_DIR}/top-hub.json"
MAX_AGE_DAYS=7

mkdir -p "${OUT_DIR}"

# 1) List top-level once (cheap; do this only when you have rate-limit headroom)
# We assume a small manifest file exists in the repo: /manifests/top-hub-latest.json
# If not, fallback to a local default.
MANIFEST_PATH="manifests/top-hub-latest.json"

# Try CDN first (no auth, bypass API rate limits)
URL="https://huggingface.co/datasets/${REPO}/resolve/main/${MANIFEST_PATH}"
if curl -fsSL --max-time 10 "${URL}" -o "${OUT_FILE}.tmp"; then
  mv "${OUT_FILE}.tmp" "${OUT_FILE}"
  echo "Fetched top-hub manifest via CDN"
else
  echo "CDN fetch failed — using local default"
  cat > "${OUT_FILE}" <<'EOF'
{
  "hub": "MOC",
  "score": 0.94,
  "connections": 128,
  "summary": "Most Operational Context hub — central to cost governance signals.",
  "updated_at": "2026-05-03T04:00:00Z",
  "source_url": "https://huggingface.co/datasets/AXENTX/KnowledgeGraph/blob/main/manifests/top-hub-latest.json"
}
EOF
fi

# Normalize/canonicalize (ensure minimal schema)
if command -v jq >/dev/null 2>&1; then
  jq '{
    hub: .hub // "MOC",
    score: (.score // 0.0 | tonumber),
    connections: (.connections // 0 | tonumber),
    summary: .summary // "Top hub for cost governance context.",
    updated_at: .updated_at // "2026-05-03T04:00:00Z",
    source_url: .source_url // "https://huggingface.co/datasets/AXENTX/KnowledgeGraph"
  }' "${OUT_FILE}" > "${OUT_FILE}.normalized" && mv "${OUT_FILE}.normalized" "${OUT_FILE}"
fi

echo "Top-hub data written to ${OUT_FILE}"
```

Make executable:

```bash
chmod +x scripts/fetch-top-hub.sh
```

Cron-safe invocation (if scheduling on Mac):

```bash
SHELL=/bin/bash
0 6 * * * /bin/bash /opt/axentx/Costinel/scripts/fetch-top-hub.sh >> /var/log/costinel-top-hub.log 2>&1
```

---

## 2. Runtime panel component (React)

File: `src/components/TopHubSignalPanel.tsx`

```tsx
import React, { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

interface TopHubData {
  hub: string;
  score: number;
  connections: number;
  summary: string;
  updated_at: string;
  source_url: string;
}

const STALE_DAYS = 7;

function isStale(updatedAt: string): boolean {
  try {
    const updated = new Date(updatedAt).getTime();
    const now = Date.now();
    return (now - updated) > STALE_DAYS * 24 * 60 * 60 * 1000;
  } catch {
    return true;
  }
}

export const TopHubSignalPanel: React.FC = () => {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // CDN-first: public/data is served statically (no auth, no runtime HF API)
    fetch('/data/top-hub.json', { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message || 'Failed to load top-hub signal');
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel loading">
        <div className="spinner" />
        <span>Loading top hub signal…</span>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="top-hub-panel error">
        <strong>Top Hub Signal</strong>
        <p>Unavailable — using default context.</p>
        <small>MOC (fallback) — central to cost governance signals.</small>
      </div>
    );
  }

  const stale = isStale(data.updated_at);

  return (
    <div className={`top-hub-panel${stale ? ' stale' : ''}`}>
      <div className="header">
        <strong>Top Hub Signal</strong>
        {stale && <span className="stale-badge">Stale</span>}
      </div>

      <div className="hub-name">{data.hub}</div>

      <div className="metrics">
        <div className="metric">
          <span className="label">Score</span>
          <span className="value">{(data.score * 100).toFixed(0)}%</span>
        </div>
        <div className="metric">
          <span className="label">Connections</span>
          <span className="value">{data.connections}</span>
        </div>
      </div>

      <p className="summary">{data.summary}</p>

      <div className="footer">
        <small>
          Updated {new Date(data.updated_at).toLocaleDateString()}
          {data.source_url && (
            <>
              {' · '}
              <a href={data.source_url} target="_blank" rel="noopener noreferrer">
                source
              </a>
            </>
          )}
        </small>
      </div>
    </div>
  );
};
```

Styles: `src/components/TopHubSignalPanel.css`

```css
.top-hub-panel {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 14px 16px;
  background: #fff;
  max-width: 320px;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
}

.top-hub-panel.loading,
.top-hub-panel.error {
