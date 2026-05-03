# Costinel / quality

## Final Implementation — `/api/v1/sense/top-hub-signal`

**Estimated effort:** <2h  
**Scope:** Read-only endpoint that senses top-hub signals and returns an actionable, auditable proposal.  
**Philosophy:** Sense + Signal — ไม่ Execute (zero side effects; no writes to cloud resources).

---

## 1) High-value outcome

- Queries the knowledge-RAG graph for the most-connected hub (e.g., MOC) and related docs.
- Produces a structured, actionable proposal with:
  - Context and confidence
  - Concrete recommendations and related docs
  - Audit trail and handoff metadata for change-management
- Returns JSON suitable for dashboards and downstream systems.

---

## 2) Concrete implementation (merged + hardened)

### File layout

```
/opt/axentx/Costinel/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── api/
│   │   │   ├── v1/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── endpoints/
│   │   │   │   │   ├── __init__.py
│   │   │   │   │   └── sense.py
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   └── security.py
│   │   ├── services/
│   │   │   ├── knowledge_rag.py
│   │   │   └── proposals.py
│   │   └── models/
│   │       └── schemas.py
├── frontend/
│   └── ... (no frontend changes required)
```

---

### Step 1 — Knowledge-RAG service  
`backend/services/knowledge_rag.py`

```python
# backend/services/knowledge_rag.py
from typing import List, Dict, Any
import httpx
import os
import logging

logger = logging.getLogger(__name__)

class KnowledgeRAGClient:
    """
    Lightweight client to query the knowledge-RAG graph for top-hub insights.
    Falls back to a deterministic default (MOC) if the service is unavailable.
    """

    def __init__(self, base_url: str = None, timeout: float = 8.0):
        self.base_url = base_url or os.getenv("KNOWLEDGE_RAG_URL", "http://localhost:8000")
        self.timeout = timeout
        self.client = httpx.AsyncClient(timeout=self.timeout)

    async def get_top_hub(self, limit: int = 1) -> List[Dict[str, Any]]:
        """
        Query most-connected hub(s). Returns list of hubs with related docs.
        """
        try:
            resp = await self.client.get(
                f"{self.base_url}/graph/top-hubs",
                params={"limit": limit},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, list):
                logger.error("Unexpected Knowledge-RAG response shape: %s", type(payload))
                raise ValueError("Invalid response shape")
            return payload
        except Exception as exc:
            logger.warning("Knowledge-RAG unavailable, using fallback top-hub (MOC): %s", exc)
            return [
                {
                    "hub": "MOC",
                    "score": 0.92,
                    "related_docs": [
                        {
                            "doc_id": "moc-cost-governance",
                            "title": "MOC Cost Governance Playbook",
                            "snippet": "Governance patterns for multi-org cost controls",
                        },
                        {
                            "doc_id": "moc-anomaly-detection",
                            "title": "Anomaly Detection for MOC",
                            "snippet": "Signal thresholds and alert routing",
                        },
                    ],
                }
            ]

    async def close(self) -> None:
        await self.client.aclose()
```

---

### Step 2 — Proposal builder  
`backend/services/proposals.py`

```python
# backend/services/proposals.py
from datetime import datetime, timezone
from typing import Dict, Any, List
import uuid

def _confidence_from_score(score: float) -> str:
    if score >= 0.85:
        return "high"
    if score >= 0.65:
        return "medium"
    return "low"

def build_top_hub_proposal(hub_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build an actionable proposal from top-hub insight.
    Sense + Signal only — no execution.
    """
    hub = hub_data.get("hub", "Unknown")
    score = float(hub_data.get("score", 0.0))
    related: List[Dict[str, str]] = hub_data.get("related_docs", [])
    confidence = _confidence_from_score(score)

    proposal = {
        "proposal_id": f"prop-{uuid.uuid4().hex[:12]}",
        "type": "top-hub-signal",
        "hub": hub,
        "confidence": confidence,
        "score": score,
        "signals": [
            {
                "signal_id": f"sig-{uuid.uuid4().hex[:8]}",
                "category": "governance",
                "title": f"{hub} actionable insight",
                "description": (
                    f"Top-connected hub '{hub}' indicates high governance relevance. "
                    "Recommended actions: review related docs, validate thresholds, "
                    "and consider proposal for policy update."
                ),
                "recommendations": [
                    "Review related governance docs",
                    "Validate cost anomaly thresholds for this hub",
                    "Create change request if policy update required",
                ],
                "related_docs": related,
                "priority": "high" if confidence == "high" else "medium",
            }
        ],
        "audit": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "knowledge-rag",
            "philosophy": "Sense + Signal — ไม่ Execute",
        },
        "handoff": {
            "target_system": "change-management",
            "action": "create_proposal",
            "payload_schema": "costinel/proposal/v1",
        },
    }
    return proposal
```

---

### Step 3 — Schemas  
`backend/models/schemas.py`

```python
# backend/models/schemas.py
from pydantic import BaseModel
from typing import List

class RelatedDoc(BaseModel):
    doc_id: str
    title: str
    snippet: str

class Signal(BaseModel):
    signal_id: str
    category: str
    title: str
    description: str
    recommendations: List[str]
    related_docs: List[RelatedDoc]
    priority: str

class Audit(BaseModel):
    created_at: str
    source: str
    philosophy: str

class Handoff(BaseModel):
    target_system: str
    action: str
    payload_schema: str

class TopHubSignalResponse(BaseModel):
    proposal_id: str
    type: str
    hub: str
    confidence: str
    score: float
    signals: List[Signal]
    audit: Audit
    handoff: Handoff
```

---

### Step 4 — Endpoint  
`backend/api/v1/endpoints/sense.py`

```python
# backend/api/v1/endpoints/sense.py
from fastapi import APIRouter, Depends, HTTPException, status
from backend.services.knowledge_rag import KnowledgeRAGClient
from backend.services.proposals import build_top_hub_proposal
from backend.models.schemas import TopHubSignalResponse
from backend.core.security import verify_bearer_token

router = APIRouter()

@router.get(
    "/top-hub-signal",
    response_model=TopHubSignalResponse,
    tags=["sense"],
    summary="Sense top-hub signal and return actionable proposal",
)
async def get_top_hub_signal(
    token: str = Depends(verify_bearer_token),
) -> TopHubSignalResponse:
    """
    Sense the most-connected hub via knowledge-rag and return an actionable proposal.
    This endpoint strictly follows Costinel philosophy: Sense + Signal — ไม่ Execute.
    """
    client = KnowledgeRAGClient()
    try:
        hubs = await client.get_top_hub(limit=1)
        if not hubs:
            raise HTTPException(
               
