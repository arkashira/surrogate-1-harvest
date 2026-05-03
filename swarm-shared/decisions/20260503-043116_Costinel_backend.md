# Costinel / backend

## Final Synthesis — CDN-First Top-Hub Signal Panel (Correct + Actionable)

**Goal**: Ship a resilient “Top Hub” signal panel into Costinel that shows the most-connected hub (e.g., “MOC”) and related docs using **CDN-fetched artifacts only**.  
**Principle**: Strict **Sense + Signal — No Execution at runtime**. Zero runtime HF API calls. No new infra, no secrets, no DB migrations.

---

### Architecture (CDN-first, resilient)

1. **Offline/Mac orchestration** (run once per knowledge refresh):
   - List repo tree (single API call) → `file-list.json`.
   - Compute top hub + related docs from local graph → `top-hub.json`.
   - Commit both to repo (or push to CDN path).

2. **Runtime (Costinel backend)**:
   - Serve `top-hub.json` as static asset or fetch via CDN at startup/refresh.
   - Expose `/api/signals/top-hub` returning `{ hub, score, relatedDocs[] }`.
   - Frontend panel consumes endpoint and renders cards.

3. **Resilience**:
   - CDN fetch with short timeout; fallback to bundled static file.
   - No HF API during runtime → immune to 429/128-commit limits.

---

### Implementation

#### 1) Offline generator (run on Mac) — `scripts/generate-top-hub.sh`
```bash
#!/usr/bin/env bash
# scripts/generate-top-hub.sh
# Usage: bash scripts/generate-top-hub.sh <date-folder>
set -euo pipefail
export SHELL=/bin/bash

DATE_FOLDER="${1:-$(date +%Y-%m-%d)}"
REPO="AXENTX/knowledge-rag"
OUT_DIR="public/signals"
mkdir -p "$OUT_DIR"

# 1) List files once (single API call)
echo "Listing repo tree for $DATE_FOLDER..."
python3 -c "
import os, json
from huggingface_hub import HfApi
api = HfApi()
files = api.list_repo_tree('$REPO', path='$DATE_FOLDER', recursive=False)
file_list = [f.rfilename for f in files if f.type == 'file']
open('$OUT_DIR/file-list.json', 'w').write(json.dumps(file_list, indent=2))
print(f'Wrote {len(file_list)} files')
"

# 2) Generate top-hub.json from local graph
python3 -c "
import json, os, re
with open('$OUT_DIR/file-list.json') as f:
    files = json.load(f)

# Heuristic: pick most frequent 3-char code in filenames as hub
candidates = {}
for fn in files:
    m = re.search(r'([A-Z]{3})', fn)
    if m:
        c = m.group(1)
        candidates[c] = candidates.get(c, 0) + 1
top = max(candidates, key=candidates.get) if candidates else 'MOC'
related = [f for f in files if top in f][:10]

payload = {
    'hub': top,
    'score': candidates.get(top, 0),
    'relatedDocs': [
        {
            'name': os.path.basename(f),
            'cdnUrl': f'https://huggingface.co/datasets/{os.environ.get(\"REPO\",\"$REPO\")}/resolve/main/{f}',
            'path': f
        } for f in related
    ],
    'generatedAt': os.popen('date -u +\"%Y-%m-%dT%H:%M:%SZ\"').read().strip()
}
open('$OUT_DIR/top-hub.json', 'w').write(json.dumps(payload, indent=2))
print('Generated top-hub.json')
"

echo "Done. Files in $OUT_DIR"
```

Make executable:
```bash
chmod +x scripts/generate-top-hub.sh
```

---

#### 2) Backend endpoint — `app/api/signals/top_hub.py`
```python
# app/api/signals/top_hub.py
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
import httpx

router = APIRouter()

STATIC_TOP_HUB = Path(__file__).resolve().parent.parent.parent.parent / "public" / "signals" / "top-hub.json"
CDN_TOP_HUB = "https://huggingface.co/datasets/AXENTX/knowledge-rag/resolve/main/signals/top-hub.json"

def load_local() -> dict:
    if STATIC_TOP_HUB.exists():
        return json.loads(STATIC_TOP_HUB.read_text())
    raise FileNotFoundError("top-hub.json not found in static bundle")

async def fetch_cdn() -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(CDN_TOP_HUB)
        resp.raise_for_status()
        return resp.json()

@router.get("/top-hub")
async def get_top_hub():
    """
    Sense + Signal endpoint.
    Returns top hub and related docs from CDN-first strategy with local fallback.
    """
    try:
        data = await fetch_cdn()
        source = "cdn"
    except Exception:
        try:
            data = load_local()
            source = "local"
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Top-hub signal unavailable") from exc

    if "hub" not in data:
        raise HTTPException(status_code=502, detail="Invalid top-hub payload")

    return {
        "hub": data["hub"],
        "score": data.get("score", 0),
        "relatedDocs": data.get("relatedDocs", []),
        "generatedAt": data.get("generatedAt"),
        "source": source,
        "sensedAt": datetime.now(timezone.utc).isoformat(),
    }
```

Register router in `app/main.py`:
```python
# app/main.py (excerpt)
from app.api.signals.top_hub import router as top_hub_router
app.include_router(top_hub_router, prefix="/api/signals", tags=["signals"])
```

---

#### 3) Frontend panel — `components/TopHubSignalPanel.tsx`
```tsx
// components/TopHubSignalPanel.tsx
'use client';
import { useEffect, useState } from 'react';

interface Doc {
  name: string;
  cdnUrl: string;
  path: string;
}

interface TopHubPayload {
  hub: string;
  score: number;
  relatedDocs: Doc[];
  generatedAt?: string;
  source?: string;
  sensedAt?: string;
}

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch('/api/signals/top-hub')
      .then((r) => r.json())
      .then((j) => {
        setData(j);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  if (loading) return <div className="p-4 text-sm text-gray-500">Loading top hub signal...</div>;
  if (!data) return <div className="p-4 text-sm text-red-500">Signal unavailable.</div>;

  return (
    <div className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-gray-900">Top Hub Signal</h3>
        <span className="text-xs text-gray-400">{data.source}</span>
      </div>

      <div className="mt-3">
        <p className="text-2xl font-bold text-gray-900">{data.hub}</p>
        <p className="text-sm text-gray-500">Score: {data.score}</p>
        {data.generatedAt && (
          <p className="text-xs text-gray-400">Generated: {data.generatedAt}</p>
        )}
      </div>

      <div className="mt-4 space-y-2">
        {data.relatedDocs.map((doc) => (
          <a
            key={doc.path}
