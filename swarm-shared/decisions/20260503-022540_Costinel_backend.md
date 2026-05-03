# Costinel / backend

**Final consolidated implementation**  
(Strongest parts from both proposals, contradictions resolved in favor of correctness + concrete actionability)

---

## 1) Scope & value (resolved)
- **Backend-first, frontend-lite**: ship a **read-only** `/api/v1/hubs/{hub}/signals` endpoint (backend) and a lightweight `TopHubSignalPanel` (frontend).  
- **Why**: backend owns data integrity, CDN-first avoids HF rate limits, frontend consumes the same contract.  
- **Deliverable**: <2h end-to-end (backend route + service + frontend panel + one-time manifest).

---

## 2) File changes (merged structure)

```
/opt/axentx/Costinel/
├── backend/
│   ├── app.py
│   ├── services/
│   │   ├── knowledge_rag.py
│   │   └── cdn.py
│   ├── models/
│   │   └── signal.py
│   └── config.py
├── data/
│   └── knowledge-graph/
│       └── top_hub_manifest.json
├── src/
│   ├── components/dashboard/
│   │   └── TopHubSignalPanel.tsx
│   ├── pages/
│   │   └── Dashboard.tsx
│   ├── mocks/
│   │   └── hub-signals.json
│   └── lib/
│       └── api.ts
└── package.json
```

---

## 3) Backend (FastAPI) — correctness fixes applied

### `backend/models/signal.py`
```python
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime

class SignalProposal(BaseModel):
    id: str
    title: str
    summary: str
    action_type: str
    impact_score: float = Field(ge=0.0, le=1.0)
    context: Dict[str, Any]
    cdn_path: Optional[str] = None
    published_at: Optional[datetime] = None

class HubSignals(BaseModel):
    hub_id: str
    hub_label: str
    total_connections: int
    proposals: List[SignalProposal]
```

### `backend/services/cdn.py`
```python
import json
import logging
import requests
from typing import List, Dict, Any

HF_DATASETS_BASE = "https://huggingface.co/datasets"
REPO = "AXENTX/Costinel-knowledge"

def cdn_get(path: str, timeout: int = 10) -> bytes:
    url = f"{HF_DATASETS_BASE}/{REPO}/resolve/main/{path}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def load_jsonl_paths(manifest_path: str) -> List[str]:
    content = cdn_get(manifest_path).decode()
    return [line.strip() for line in content.splitlines() if line.strip()]

def fetch_proposals_for_hub(hub_id: str, paths: List[str], limit: int = 3) -> List[Dict[str, Any]]:
    proposals: List[Dict[str, Any]] = []
    for p in paths:
        try:
            raw = cdn_get(p).decode()
        except Exception as ex:
            logging.warning("CDN fetch failed %s: %s", p, ex)
            continue

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue

            if item.get("hub_id") == hub_id and item.get("action_type"):
                proposals.append(item)
                if len(proposals) >= limit:
                    return proposals
    return proposals
```

### `backend/services/knowledge_rag.py`
```python
import json
import logging
import requests
from typing import Optional
from .cdn import load_jsonl_paths, fetch_proposals_for_hub
from models.signal import HubSignals, SignalProposal

TOP_HUB_MANIFEST = "knowledge-graph/top_hub_manifest.json"

def resolve_top_hub(manifest_path: str = TOP_HUB_MANIFEST) -> Optional[str]:
    try:
        url = f"https://huggingface.co/datasets/AXENTX/Costinel-knowledge/resolve/main/{manifest_path}"
        content = requests.get(url, timeout=10).decode()
        data = json.loads(content)
        hub_id = data.get("top_hub_id")
        if hub_id:
            return str(hub_id)
    except Exception:
        logging.exception("Failed to resolve top hub from manifest")
    return "MOC"

def get_hub_signals(hub_id: Optional[str] = None, limit: int = 3) -> HubSignals:
    hub_id = hub_id or resolve_top_hub()
    paths = load_jsonl_paths("knowledge-graph/proposals/2026-05-03/paths.jsonl")
    raw = fetch_proposals_for_hub(hub_id, paths, limit=limit)

    # Build proposals safely
    proposals = [
        SignalProposal(
            id=item["id"],
            title=item["title"],
            summary=item["summary"],
            action_type=item["action_type"],
            impact_score=float(item.get("impact_score", 0.0)),
            context=item.get("context", {}),
            cdn_path=item.get("cdn_path"),
            published_at=item.get("published_at")
        )
        for item in raw
    ]

    # Derive label + connections from first valid item (fallback)
    label = "MOC"
    connections = 0
    if raw:
        first = raw[0]
        label = str(first.get("hub_label", hub_id.upper()))
        connections = int(first.get("total_connections", 0))

    return HubSignals(
        hub_id=hub_id,
        hub_label=label,
        total_connections=connections,
        proposals=proposals
    )
```

### `backend/app.py` (route)
```python
from fastapi import FastAPI, HTTPException
from services.knowledge_rag import get_hub_signals
from models.signal import HubSignals

app = FastAPI(title="Costinel API")

@app.get("/api/v1/hubs/{hub_id}/signals", response_model=HubSignals)
def read_hub_signals(hub_id: str = None):
    try:
        return get_hub_signals(hub_id=hub_id, limit=3)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Signal resolution failed: {exc}")
```

---

## 4) Frontend panel (React + Tailwind)

### `src/lib/api.ts`
```ts
export interface SignalProposal {
  id: string;
  title: string;
  summary: string;
  action_type: string;
  impact_score: number;
  context: Record<string, any>;
  cdn_path?: string;
  published_at?: string;
}

export interface HubSignals {
  hub_id: string;
  hub_label: string;
  total_connections: number;
  proposals: SignalProposal[];
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export async function fetchHubSignals(hubId?: string): Promise<HubSignals> {
  const url = hubId
    ? `${API_BASE}/api/v1/hubs/${hubId}/signals`
    : `${API_BASE}/api/v1/hubs/MOC/signals`;
  const res = await fetch(url);
  if (!res.ok) throw new Error('Failed to fetch hub signals');
  return res.json();
}
```

### `src/components/dashboard/TopHubSignalPanel.tsx`
```tsx
'use client';

import React, { useEffect, useState } from 'react';
import { TrendingUp, AlertCircle } from 'lucide-react';
import { fetchHubSignals, HubSignals, SignalProposal } from '@/lib/api';

interface Props {
  hubName?: string;
}

const actionIcons: Record<string, React.ReactNode> = {
  RI_COVERAGE: <TrendingUp className="h-4 w-4 text-blue-500" />,
  IDLE_STOP: <AlertCircle className="h-4 w-4 text-amber-500" />,
};

export default function TopHubSignalPanel({ hubName
