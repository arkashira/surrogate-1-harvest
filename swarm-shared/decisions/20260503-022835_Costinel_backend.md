# Costinel / backend

## Highest-Value Incremental Improvement (<2h)

**Add a Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-implication signals — CDN-first, read-only, resilient, and deployable in <2h.

- **Why this ships fast**: no auth/rate-limit issues (CDN bypass), no schema migrations, no new cloud costs, and reuses existing knowledge-rag graph.
- **What it enables**: product teams see the single highest-leverage cost signal (MOC) with 3 concrete recommendations before any deeper drill.
- **Non-goals for this increment**: execution/actuation, write paths, or complex ML ranking — keep it read-only and signal-only (Sense + Signal).

---

## Implementation Plan (≤2h)

1. **Locate/confirm backend entrypoint**  
   - FastAPI app likely in `main.py` or `app/` (common for Costinel).  
   - Add one lightweight endpoint: `GET /api/signal/top-hub`.

2. **Implement CDN-first knowledge-rag fetch**  
   - Use `list_repo_tree` once (from orchestration machine) to get latest `top-hub.json` path (e.g., `knowledge-rag/top-hub/MOC.json`).  
   - Embed the file list in the endpoint or fetch via CDN URL:  
     `https://huggingface.co/datasets/{repo}/resolve/main/knowledge-rag/top-hub/MOC.json`  
   - Cache in memory (TTL 300s) to avoid repeated CDN hits and to survive transient CDN issues.

3. **Resilience/proxy layer (lightweight)**  
   - If CDN fetch fails, fall back to a local bundled snapshot (`static/fallback/top-hub-MOC.json`).  
   - Return 200 with `{ hub, signals[], ts, source }`.

4. **Frontend panel (minimal)**  
   - Add a card to the dashboard: “Top-Hub Signal (MOC)”.  
   - Show hub name + 3 signals as concise bullets with cost impact (↑/↓) and context.  
   - No interactive writes; link to full report if exists.

5. **Tests & deploy**  
   - One unit test for endpoint shape.  
   - Deploy via existing Docker compose (no infra changes).  
   - Verify CDN bypass works (no 429s).

---

## Code Snippets

### Backend: FastAPI endpoint (app/main.py or app/api/signals.py)

```python
# app/api/signals.py
import asyncio
import json
import os
import time
from typing import Any, Dict

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

# Config via env (override in docker-compose if needed)
HF_DATASET_OWNER = os.getenv("HF_DATASET_OWNER", "axentx")
HF_DATASET_NAME = os.getenv("HF_DATASET_NAME", "costinel-knowledge")
HUB_DEFAULT = os.getenv("TOP_HUB_DEFAULT", "MOC")
CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_OWNER}/{HF_DATASET_NAME}/resolve/main"
LOCAL_FALLBACK = os.getenv("LOCAL_FALLBACK_PATH", "static/fallback/top-hub-MOC.json")

# Simple in-memory cache
_CACHE: Dict[str, Any] = {"payload": None, "ts": 0}
TTL_SECONDS = int(os.getenv("SIGNAL_CACHE_TTL", 300))

async def _fetch_cdn_top_hub(hub: str) -> Dict[str, Any]:
    url = f"{CDN_BASE}/knowledge-rag/top-hub/{hub}.json"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"CDN fetch failed: {resp.status_code}")
        return resp.json()

def _load_local_fallback(hub: str) -> Dict[str, Any]:
    path = LOCAL_FALLBACK.replace("MOC", hub) if "MOC" in LOCAL_FALLBACK else LOCAL_FALLBACK
    if not os.path.exists(path):
        raise HTTPException(status_code=503, detail="No local fallback available")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

@router.get("/top-hub")
async def get_top_hub_signal(hub: str = HUB_DEFAULT) -> JSONResponse:
    now = time.time()
    if _CACHE["payload"] and (now - _CACHE["ts"]) < TTL_SECONDS:
        return JSONResponse(content=_CACHE["payload"])

    try:
        data = await _fetch_cdn_top_hub(hub)
        source = "cdn"
    except Exception:
        # graceful degradation
        data = _load_local_fallback(hub)
        source = "local-fallback"

    payload = {
        "hub": hub,
        "signals": data.get("signals", [])[:3],  # top 3 actionable signals
        "generated_at": data.get("generated_at", None),
        "source": source,
        "cached_at": now,
    }
    _CACHE["payload"] = payload
    _CACHE["ts"] = now
    return JSONResponse(content=payload)
```

### Docker-compose snippet (ensure network + env)

```yaml
# docker-compose.yml (excerpt)
services:
  backend:
    build: ./backend
    environment:
      - HF_DATASET_OWNER=axentx
      - HF_DATASET_NAME=costinel-knowledge
      - TOP_HUB_DEFAULT=MOC
      - SIGNAL_CACHE_TTL=300
    ports:
      - "8000:8000"
```

### Frontend panel (React/Next.js example — minimal)

```tsx
// components/TopHubSignalPanel.tsx
import useSWR from 'swr';

const fetcher = (url: string) => fetch(url).then((r) => r.json());

export default function TopHubSignalPanel() {
  const { data, error } = useSWR('/api/signal/top-hub', fetcher, { refreshInterval: 300000 });

  if (error) return <div className="p-4 text-red-600">Unable to load top-hub signal.</div>;
  if (!data) return <div className="p-4">Loading top-hub signal...</div>;

  const { hub, signals = [], source } = data;

  return (
    <div className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">Top-Hub Signal ({hub})</h3>
        <span className="text-xs text-gray-400">{source}</span>
      </div>
      <ul className="mt-3 space-y-2">
        {signals.map((s: any, i: number) => (
          <li key={i} className="text-sm text-gray-700">
            <span className="font-medium">{s.title}</span>
            <p className="text-gray-500">{s.context}</p>
            {s.costImpact && (
              <span className={`text-xs font-semibold ${s.costImpact.startsWith('+') ? 'text-red-600' : 'text-green-600'}`}>
                {s.costImpact}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
```

### Local fallback example (static/fallback/top-hub-MOC.json)

```json
{
  "generated_at": "2026-05-03T00:00:00Z",
  "signals": [
    {
      "title": "Unattached EBS volumes",
      "context": "12 unattached volumes across prod accounts (~$460/mo). Recommend snapshot + delete after 7-day retention.",
      "costImpact": "+$460/mo"
    },
    {
      "title": "Low RIs coverage on m5.large",
      "context": "Current coverage 34%. Buying 1-yr No Upfront RIs would save ~$2
