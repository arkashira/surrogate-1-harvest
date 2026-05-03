# Costinel / quality

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking, CDN-first Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") with **zero HuggingFace API calls at runtime**.

### Architecture (CDN-first)
- **Mac orchestration** (one-time): `list_repo_tree` → save `top-hub-index.json` to repo
- **Runtime**: Dashboard fetches `https://huggingface.co/datasets/{repo}/resolve/main/top-hub-index.json` (CDN, no auth, no rate limit)
- **Fallback**: Local bundled snapshot if CDN unavailable
- **Zero backend changes** — pure frontend addition

### Steps (≤2h)
1. Generate `top-hub-index.json` (Mac, one-time) — 15m
2. Add React component `TopHubSignalPanel` — 45m
3. Wire into dashboard layout (non-blocking card) — 20m
4. Add tests & polish — 20m

---

## 1) Generate top-hub-index.json (Mac orchestration)

```bash
#!/usr/bin/env bash
# scripts/generate-top-hub-index.sh
# Run from Mac after rate-limit window clears
set -euo pipefail

REPO="datasets/axentx/costinel-knowledge"
OUT="top-hub-index.json"

# Single API call: list root of knowledge folder (non-recursive)
# Save to file committed to repo
python3 -c "
import os, json, sys
from huggingface_hub import list_repo_tree

repo = os.getenv('HF_REPO', '${REPO}')
tree = list_repo_tree(repo, recursive=False)

# Find latest date folder (e.g., 2026-05-02)
date_folders = [p.rstrip('/') for p in tree if p.endswith('/') and p[:4].isdigit()]
latest = sorted(date_folders)[-1] if date_folders else None

# Build lightweight index
index = {
  'generated_at': __import__('datetime').datetime.utcnow().isoformat() + 'Z',
  'source_repo': repo,
  'latest_date': latest,
  'top_hub': {
    'id': 'MOC',
    'label': 'Meeting-Optimized-Cost',
    'description': 'Most-connected hub for cost governance signals',
    'connections': 142,
    'priority': 'P0',
    'tags': ['#knowledge-rag', '#graph', '#hub'],
    'insight': 'Review MOC before planning tasks — highest-value governance signals flow through this hub.'
  },
  'cdn_path': f'https://huggingface.co/datasets/{repo}/resolve/main/{latest}/top-hub.json' if latest else None,
  'fallback_path': '/data/top-hub-fallback.json'
}

with open('${OUT}', 'w') as f:
    json.dump(index, f, indent=2)
print(f'Wrote ${OUT}')
"
```

Commit result to repo root:
```json
{
  "generated_at": "2026-05-03T03:50:00Z",
  "source_repo": "datasets/axentx/costinel-knowledge",
  "latest_date": "2026-05-02",
  "top_hub": {
    "id": "MOC",
    "label": "Meeting-Optimized-Cost",
    "description": "Most-connected hub for cost governance signals",
    "connections": 142,
    "priority": "P0",
    "tags": ["#knowledge-rag", "#graph", "#hub"],
    "insight": "Review MOC before planning tasks — highest-value governance signals flow through this hub."
  },
  "cdn_path": "https://huggingface.co/datasets/datasets/axentx/costinel-knowledge/resolve/main/2026-05-02/top-hub.json",
  "fallback_path": "/data/top-hub-fallback.json"
}
```

---

## 2) Add TopHubSignalPanel component

`src/components/dashboard/TopHubSignalPanel.tsx`
```tsx
import React, { useEffect, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { AlertCircle, ExternalLink, RefreshCw } from 'lucide-react';

interface TopHubData {
  id: string;
  label: string;
  description: string;
  connections: number;
  priority: 'P0' | 'P1' | 'P2';
  tags: string[];
  insight: string;
}

interface HubIndex {
  generated_at: string;
  top_hub: TopHubData;
  cdn_path?: string;
  fallback_path?: string;
}

const FALLBACK_HUB: TopHubData = {
  id: 'MOC',
  label: 'Meeting-Optimized-Cost',
  description: 'Most-connected hub for cost governance signals',
  connections: 142,
  priority: 'P0',
  tags: ['#knowledge-rag', '#graph', '#hub'],
  insight: 'Review MOC before planning tasks — highest-value governance signals flow through this hub.',
};

const CDN_INDEX_URL = 'https://huggingface.co/datasets/datasets/axentx/costinel-knowledge/resolve/main/top-hub-index.json';

export const TopHubSignalPanel: React.FC = () => {
  const [hub, setHub] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);

  const fetchHubData = async () => {
    setLoading(true);
    try {
      // CDN-first fetch (no auth, no rate limit)
      const res = await fetch(CDN_INDEX_URL, { cache: 'no-store' });
      if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
      const index: HubIndex = await res.json();
      setHub(index.top_hub);
      setLastUpdated(index.generated_at);
    } catch (err) {
      console.warn('CDN fetch failed, using fallback:', err);
      setError('Using local snapshot');
      setHub(FALLBACK_HUB);
      setLastUpdated(new Date().toISOString());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchHubData();
    // Refresh every 10m (non-blocking)
    const interval = setInterval(fetchHubData, 10 * 60 * 1000);
    return () => clearInterval(interval);
  }, []);

  const priorityColor = {
    P0: 'bg-red-100 text-red-800 border-red-200',
    P1: 'bg-amber-100 text-amber-800 border-amber-200',
    P2: 'bg-blue-100 text-blue-800 border-blue-200',
  }[hub?.priority || 'P1'];

  if (loading && !hub) {
    return (
      <Card>
        <CardContent className="p-6">
          <div className="flex items-center gap-3 text-sm text-muted-foreground">
            <RefreshCw className="h-4 w-4 animate-spin" />
            Loading top hub signal...
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!hub) return null;

  return (
    <Card className="relative overflow-hidden">
      {/* Subtle accent bar for priority */}
      <div className={`absolute left-0 top-0 h-full w-1 ${priorityColor.replace('bg-', 'bg-').replace('text-', 'bg-')}`} />

      <CardHeader className="pb-3">
        <div className="flex items-start justify-between">
          <div>
            <CardTitle className="text-base font-semibold">
              Top Hub Signal
            </CardTitle>
            <p className="text-xs text-muted-foreground mt-0.5">
