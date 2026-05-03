# Costinel / backend

## Final Synthesis — Costinel “Top-Hub Signal” (backend-backed, frontend card)

**Chosen approach**: Backend-for-frontend.  
- Add a lightweight, read-only endpoint `/api/signals/top-hub` that queries the knowledge-rag graph, returns the most-connected hub + 3 contextual signals, and caches for 5 minutes.  
- Keep the frontend card strictly presentational (loading/error/empty states) and consume that endpoint.  
- This avoids duplicating graph logic in the browser, keeps auth/server concerns server-side, and still fits a ≤2h timebox.

**Patterns applied**: #knowledge-rag #graph #hub #Sense+Signal

---

### 1) API contract (backend response)

```json
{
  "hub": {
    "id": "MOC",
    "label": "MOC (Mechanism of Change)",
    "type": "hub",
    "score": 42,
    "snippet": "Most-connected hub across cost governance decisions and policy graph.",
    "href": "/knowledge-rag/hubs/MOC",
    "tags": ["governance", "policy", "change-management"]
  },
  "signals": [
    {
      "id": "S1",
      "label": "RI Coverage Gap >30%",
      "type": "signal",
      "score": 0.85,
      "snippet": "Detected across prod accounts; potential 18% savings if converted to 1-yr partial-upfront.",
      "href": "/recommendations/ri-coverage",
      "tags": ["AWS", "RI", "savings"]
    }
    // ... 2 more
  ]
}
```

- `score` on hub is an integer connection count; on signals it is a 0–1 relevance.  
- All fields optional for graceful degradation.

---

### 2) Backend implementation (FastAPI example)

```python
# services/knowledge_rag.py
from functools import lru_cache
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# Assuming an existing graph interface: graph.nodes, graph.edges, graph.degree
class KnowledgeRAG:
    def __init__(self, graph):
        self.graph = graph

    def top_hub(self, limit: int = 1) -> Optional[Dict]:
        if not self.graph.nodes:
            return None
        top = max(self.graph.nodes.items(), key=lambda kv: self.graph.degree(kv[0]))
        node = top[1]
        return {
            "id": node.get("id"),
            "label": node.get("label") or node.get("id"),
            "type": "hub",
            "score": int(self.graph.degree(node.get("id"))),
            "snippet": node.get("snippet"),
            "href": node.get("href"),
            "tags": node.get("tags", []),
        }

    def contextual_signals(self, hub_id: str, limit: int = 3) -> List[Dict]:
        # Return highest-affinity neighbors that are not hubs
        neighbors = [
            (nid, self.graph.nodes[nid])
            for nid in self.graph.neighbors(hub_id)
            if self.graph.nodes[nid].get("type") != "hub"
        ]
        neighbors.sort(key=lambda kv: kv[1].get("affinity", 0.0), reverse=True)
        out = []
        for nid, node in neighbors[:limit]:
            out.append(
                {
                    "id": node.get("id", nid),
                    "label": node.get("label") or nid,
                    "type": node.get("type", "signal"),
                    "score": float(node.get("affinity", 0.0)),
                    "snippet": node.get("snippet"),
                    "href": node.get("href"),
                    "tags": node.get("tags", []),
                }
            )
        return out


# api/signals.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

router = APIRouter()

# In-memory TTL cache (simple; replace with redis/mcache in prod)
_cache = {"value": None, "expires_at": None}

class SignalItem(BaseModel):
    id: str
    label: str
    type: str
    score: float
    snippet: Optional[str] = None
    href: Optional[str] = None
    tags: List[str] = []

class HubItem(BaseModel):
    id: str
    label: str
    type: str
    score: int
    snippet: Optional[str] = None
    href: Optional[str] = None
    tags: List[str] = []

class TopHubSignalResponse(BaseModel):
    hub: Optional[HubItem]
    signals: List[SignalItem] = []

@router.get("/signals/top-hub", response_model=TopHubSignalResponse)
def get_top_hub_signal():
    now = datetime.utcnow()
    if _cache["value"] and _cache["expires_at"] and now < _cache["expires_at"]:
        return _cache["value"]

    try:
        rag = KnowledgeRAG(graph=app.state.knowledge_graph)  # attach graph at startup
        hub = rag.top_hub()
        signals = rag.contextual_signals(hub["id"], limit=3) if hub else []
        payload = {"hub": hub, "signals": signals}
        _cache["value"] = payload
        _cache["expires_at"] = now + timedelta(minutes=5)
        return payload
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to compute top-hub signal: {exc}")
```

- Correctness choices:  
  - Hub `score` is integer degree; signal `score` is 0–1 affinity.  
  - Cache TTL 5 minutes balances freshness and load.  
  - Graceful fallback: endpoint returns `hub: null, signals: []` on failure; frontend handles empty state.

---

### 3) Frontend contract (TypeScript)

```ts
// src/components/CostinelTopHubSignalCard.types.ts
export interface HubSignal {
  id: string;
  label: string;
  type: 'hub' | 'signal';
  score: number;
  snippet?: string;
  href?: string;
  tags?: string[];
}

export interface TopHubSignalResponse {
  hub: HubSignal | null;
  signals: HubSignal[];
}

export interface TopHubSignalCardProps {
  data?: TopHubSignalResponse | null;
  loading?: boolean;
  error?: string | null;
  onRefresh?: () => void;
}
```

---

### 4) Frontend implementation (React + Tailwind)

```tsx
// src/components/CostinelTopHubSignalCard.tsx
import React from 'react';
import { TopHubSignalCardProps, HubSignal } from './CostinelTopHubSignalCard.types';

const HubPill: React.FC<{ item: HubSignal; isHub?: boolean }> = ({ item, isHub = false }) => (
  <div
    className={`rounded-lg border p-3 ${
      isHub
        ? 'border-amber-200 bg-amber-50'
        : 'border-slate-200 bg-white hover:border-slate-300 transition-colors'
    }`}
  >
    <div className="flex items-start justify-between gap-2">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span
            className={`truncate font-semibold text-sm ${
              isHub ? 'text-amber-900' : 'text-slate-900'
            }`}
          >
            {item.label}
          </span>
          <span
            className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${
              item.score >= 0.8
                ? 'bg-green-100 text-green-800'
                : item.score >= 0.6
                ? 'bg-amber-100 text-amber-800'
                : 'bg-slate-100 text-slate-700'
            }`}
          >
            {isHub ? item.score : `${Math.round(item.score * 100)}%`}
          </span>
        </div>

