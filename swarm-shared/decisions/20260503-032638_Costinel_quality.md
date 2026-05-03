# Costinel / quality

## Costinel — Quality Incremental: CDN-First Top-Hub Signal Panel (<2h)

**Chosen approach:**  
A non-blocking Top-Hub Signal Panel that surfaces the most-connected hub (e.g., “MOC”) using CDN-first data baked at build/orchestration time. Runtime dashboard loads make **zero HF API calls**, fallbacks gracefully, and respect rate-limit/CDN bypass patterns.

---

## 1. Implementation Plan (≤2h)

| Step | Owner | Time | Details |
|------|-------|------|---------|
| 1 | Engineer | 10m | Create `scripts/build-top-hub-index.js` — Mac orchestration script that runs post-market-analysis or on CI. Uses HF CDN URLs only (`resolve/main/...`) to fetch latest `knowledge-rag/top-hub.json` and `hub-graph-lite.json`. |
| 2 | Engineer | 20m | Add deterministic file-list JSON (`public/data/hub-index.json`) produced by script; embed into repo at build time (or mount via docker volume). |
| 3 | Engineer | 20m | Implement React component `TopHubSignalPanel` in `src/components/dashboard/TopHubSignalPanel.tsx`. Loads `hub-index.json` via `fetch('/data/hub-index.json')` (CDN/static). Zero HF API calls at runtime. |
| 4 | Engineer | 20m | Add graceful fallbacks: if index missing → show “Signal unavailable”; if stale (>24h) → show “Signal may be outdated”. |
| 5 | Engineer | 20m | Wire into dashboard layout (non-blocking, collapsible panel). Add lightweight polling (60s) for dev; prod uses manual refresh or build-time embed. |
| 6 | Engineer | 20m | Update Dockerfile/compose to run build script during image build (or CI) so image contains baked index. |
| 7 | Engineer | 10m | Add cron-safe wrapper for Mac orchestration (`scripts/run-top-hub-index.sh`) with proper shebang, executable bit, and `SHELL=/bin/bash` note in docs. |

**Total:** ~2h (including tests and polish).

---

## 2. Code Snippets

### 2.1 Mac orchestration script (CDN-first, zero HF API at runtime)
`scripts/build-top-hub-index.js`
```js
#!/usr/bin/env node
/**
 * Build-time script (run on Mac/CI) to produce public/data/hub-index.json
 * Uses HF CDN URLs only — no Authorization, bypasses API rate limits.
 *
 * Usage:
 *   node scripts/build-top-hub-index.js \
 *     --repo "AXENTX/KnowledgeRag" \
 *     --out "public/data/hub-index.json"
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import fetch from 'node-fetch';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const REPO = process.argv.includes('--repo')
  ? process.argv[process.argv.indexOf('--repo') + 1]
  : 'AXENTX/KnowledgeRag';
const OUT = process.argv.includes('--out')
  ? process.argv[process.argv.indexOf('--out') + 1]
  : path.join(__dirname, '../public/data/hub-index.json');

const CDN_BASE = `https://huggingface.co/datasets/${REPO}/resolve/main`;

async function fetchJsonCdn(filePath) {
  const url = `${CDN_BASE}/${filePath}`;
  const res = await fetch(url, { method: 'GET' });
  if (!res.ok) throw new Error(`CDN fetch failed: ${url} ${res.status}`);
  return res.json();
}

async function build() {
  try {
    // Fetch minimal graph + top-hub signal from CDN
    const [topHub, graphLite] = await Promise.allSettled([
      fetchJsonCdn('knowledge-rag/top-hub.json'),
      fetchJsonCdn('knowledge-rag/hub-graph-lite.json'),
    ]);

    const index = {
      generatedAt: new Date().toISOString(),
      repo: REPO,
      topHub: topHub.status === 'fulfilled' ? topHub.value : null,
      graphLite: graphLite.status === 'fulfilled' ? graphLite.value : null,
      note: 'CDN-first index — zero HF API calls at runtime',
    };

    fs.mkdirSync(path.dirname(OUT), { recursive: true });
    fs.writeFileSync(OUT, JSON.stringify(index, null, 2));
    console.log(`✅ Built hub index -> ${OUT}`);
  } catch (err) {
    console.error('❌ Failed to build hub index:', err.message);
    // Still write a safe fallback so runtime doesn't crash
    const fallback = {
      generatedAt: new Date().toISOString(),
      repo: REPO,
      topHub: null,
      graphLite: null,
      error: String(err.message),
    };
    fs.mkdirSync(path.dirname(OUT), { recursive: true });
    fs.writeFileSync(OUT, JSON.stringify(fallback, null, 2));
    process.exit(0); // non-fatal for build
  }
}

build();
```

### 2.2 Bash wrapper (cron-safe)
`scripts/run-top-hub-index.sh`
```bash
#!/usr/bin/env bash
# Wrapper for cron/systemd. Ensures proper environment.
# Add to crontab with SHELL=/bin/bash if scheduling.

set -euo pipefail
cd "$(dirname "$0")/.."

# Use project node (or fallback)
exec node scripts/build-top-hub-index.js \
  --repo "AXENTX/KnowledgeRag" \
  --out "public/data/hub-index.json"
```
Make executable:
```bash
chmod +x scripts/run-top-hub-index.sh
```

### 2.3 React Top-Hub Signal Panel
`src/components/dashboard/TopHubSignalPanel.tsx`
```tsx
import React, { useEffect, useState } from 'react';
import { Alert, Card, Spin, Tag, Typography } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';

const { Text, Paragraph } = Typography;

interface HubIndex {
  generatedAt: string;
  repo: string;
  topHub: { name: string; id: string; connections: number; summary?: string } | null;
  graphLite: any | null;
  error?: string;
}

export const TopHubSignalPanel: React.FC = () => {
  const [index, setIndex] = useState<HubIndex | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetch('/data/hub-index.json', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('Failed to load'))))
      .then((data) => {
        if (mounted) setIndex(data);
      })
      .catch((err) => {
        if (mounted) setIndex({ generatedAt: new Date().toISOString(), repo: '', topHub: null, graphLite: null, error: err.message });
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <Card size="small" title="Top-Hub Signal">
        <Spin size="small" />
      </Card>
    );
  }

  const stale = index?.generatedAt && Date.now() - new Date(index.generatedAt).getTime() > 24 * 3600 * 1000;
  const topHub = index?.topHub;

  return (
    <Card
      size="small"
      title="Top-Hub Signal"
      extra={
        <Tag color={stale ? 'orange' : topHub ? 'green' : 'red'}>
          {stale ? 'Stale' : topHub ? 'Live' : 'Unavailable'}
        </Tag>
      }
    >
      {index?.error && (
        <Alert type="warning" message="Signal unavailable" description={index.error} showIcon icon={<InfoCircleOutlined />} />
      )}

      {!topHub && !index?.error && <Alert type="info" message="No hub signal available" showIcon />}

      {top
