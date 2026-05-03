# Costinel / backend

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

### Scope (single highest-value deliverable)
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that:
- Detects and surfaces top-connected knowledge-hub signals (e.g., MOC) before planning tasks  
- Uses CDN-bypass pattern for zero-API-overhead during render  
- Graceful fallback to cached/local stub when CDN/knowledge unavailable  
- Non-blocking UI so dashboard render never stalls on knowledge fetch  
- Renders in <100ms, never blocks main dashboard  

### Architecture (fits existing patterns)
- **Backend**: FastAPI endpoint `/api/signals/top-hub` → returns `{hub, score, signals[], source, ts}`  
- **Data path**:  
  - Pre-list knowledge files once (Mac orchestration) → save `top_hub_files.json`  
  - CDN fetch at runtime via `https://huggingface.co/datasets/axentx/knowledge/resolve/main/...` (no auth, no rate-limit)  
  - Project to `{hub, score, signals, context}` only at parse time  
- **Frontend**: React panel component with SWR + suspense fallback; skeleton while loading  
- **Cron hygiene**: If any wrapper script is added, use `#!/usr/bin/env bash`, `chmod +x`, invoke via `bash`, and set `SHELL=/bin/bash` in crontab  

---

### Files to create/modify (all paths relative to `/opt/axentx/Costinel`)

1. `backend/api/signals.py` — FastAPI router for top-hub signals  
2. `backend/services/knowledge_service.py` — CDN-first loader + projection  
3. `frontend/src/components/TopHubSignalPanel.tsx` — Non-blocking UI panel  
4. `frontend/src/hooks/useTopHubSignals.ts` — SWR fetcher  
5. `scripts/list_top_hub_files.py` — Mac orchestration helper (run once/nightly)  
6. `config/cdn_file_list.json` — Embedded file list for Lightning training/CDN-only mode  
7. Update `backend/main.py` to include router  
8. Update dashboard layout to include panel  

---

### Code snippets

#### 1) `backend/services/knowledge_service.py`
```python
import json
import os
from pathlib import Path
from typing import List, Dict, Any
import httpx
from datetime import datetime, timezone

KNOWLEDGE_REPO = "axentx/knowledge"
CDN_BASE = f"https://huggingface.co/datasets/{KNOWLEDGE_REPO}/resolve/main"
FILE_LIST_PATH = Path(__file__).parent.parent.parent / "config" / "cdn_file_list.json"
CACHE_TTL = 300  # seconds

class KnowledgeService:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=10.0)
        self._file_list: List[str] = []
        self._load_file_list()

    def _load_file_list(self):
        if FILE_LIST_PATH.exists():
            with open(FILE_LIST_PATH) as f:
                self._file_list = json.load(f)
        else:
            # Fallback: minimal stub so service never crashes
            self._file_list = ["top_hub/MOC.json"]

    async def fetch_cdn_json(self, path: str) -> Dict[str, Any]:
        url = f"{CDN_BASE}/{path}"
        resp = await self.http.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()

    async def get_top_hub_signals(self, limit: int = 5) -> Dict[str, Any]:
        signals = []
        errors = []

        # Prefer top_hub/ files; fallback to any available
        candidates = [p for p in self._file_list if p.startswith("top_hub/")] or self._file_list[:10]

        for rel_path in candidates[:limit]:
            try:
                data = await self.fetch_cdn_json(rel_path)
                # Project to minimal shape at parse time
                projected = {
                    "hub": data.get("hub") or Path(rel_path).stem.upper(),
                    "score": float(data.get("score", 0.0)),
                    "signals": data.get("signals", [])[:3],
                    "context": data.get("context", ""),
                    "source": f"cdn:{rel_path}",
                }
                signals.append(projected)
            except Exception as exc:
                errors.append(f"{rel_path}: {exc}")

        return {
            "top_hub": signals[0]["hub"] if signals else "MOC",
            "signals": sorted(signals, key=lambda x: x["score"], reverse=True),
            "source": "cdn",
            "ts": datetime.now(timezone.utc).isoformat(),
            "errors": errors,
        }

    async def close(self):
        await self.http.aclose()

knowledge_service = KnowledgeService()
```

#### 2) `backend/api/signals.py`
```python
from fastapi import APIRouter, HTTPException
from backend.services.knowledge_service import knowledge_service

router = APIRouter(prefix="/signals", tags=["signals"])

@router.get("/top-hub")
async def get_top_hub():
    try:
        return await knowledge_service.get_top_hub_signals(limit=5)
    except Exception as exc:
        # Graceful fallback stub so UI never breaks
        return {
            "top_hub": "MOC",
            "signals": [
                {"hub": "MOC", "score": 0.0, "signals": ["Review cloud governance patterns"], "context": "Fallback: CDN unavailable", "source": "fallback"}
            ],
            "source": "fallback",
            "ts": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "errors": [str(exc)],
        }
```

#### 3) `backend/main.py` (add router)
```python
from fastapi import FastAPI
from backend.api.signals import router as signals_router

app = FastAPI()
app.include_router(signals_router)
# ... existing routes
```

#### 4) `frontend/src/hooks/useTopHubSignals.ts`
```ts
import useSWR from 'swr';

const fetcher = (url: string) => fetch(url).then(r => r.json());

export function useTopHubSignals() {
  return useSWR('/api/signals/top-hub', fetcher, {
    revalidateOnFocus: false,
    revalidateOnReconnect: true,
    refreshInterval: 300_000, // 5m
    fallbackData: {
      top_hub: 'MOC',
      signals: [{ hub: 'MOC', score: 0, signals: ['Review cloud governance patterns'], context: 'Loading...', source: 'fallback' }],
      source: 'fallback',
      ts: new Date().toISOString(),
    },
  });
}
```

#### 5) `frontend/src/components/TopHubSignalPanel.tsx`
```tsx
import React from 'react';
import { useTopHubSignals } from '../hooks/useTopHubSignals';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';

export function TopHubSignalPanel() {
  const { data, error } = useTopHubSignals();

  if (error) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium">Top Hub Signals</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">Unable to load signals</p>
        </CardContent>
      </Card>
    );
  }

  const signals = data?.signals || [];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm font-medium flex items-center justify-between">
          <span>Top Hub Signals</span>
          <Badge variant="secondary" className="text-xs">{data?.top_hub || 'MOC'}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="space-y-
