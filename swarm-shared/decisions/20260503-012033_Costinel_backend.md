# Costinel / backend

## Implementation Plan — Costinel Backend: `/api/v1/sense/top-hub-signal`

**Estimated effort:** <2h  
**Scope:** Read-only endpoint that senses top-hub signals and returns an actionable, auditable proposal.  
**Philosophy:** Sense + Signal — ไม่ Execute (zero side effects; no writes to cloud resources).

### High-value incremental improvement
Expose a single GET endpoint that:
1. Queries the knowledge-rag graph for the most-connected hub (e.g., "MOC") and top related docs.
2. Produces a structured signal with context, confidence, and recommended next actions.
3. Returns an auditable payload (requestId, timestamp, modelVersion, provenance) so frontend can render a proposal panel.

### Implementation steps (concrete)

1) Add FastAPI route `GET /api/v1/sense/top-hub-signal`
2) Implement lightweight service that calls knowledge-rag (local or via internal SDK) to fetch:
   - top hub node (highest degree or pagerank)
   - top 5–7 related documents/insights
   - short rationale and suggested signal actions
3) Normalize to internal `TopHubSignal` model and return 200 with stable schema.
4) Add minimal observability (requestId, timing, structured logs).
5) No database writes; all data sourced from graph/rag and returned read-only.

### Code snippets

`costinel/api/v1/sense/top_hub_signal.py`
```python
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from costinel.sense.top_hub import sense_top_hub_signal

router = APIRouter(prefix="/api/v1/sense", tags=["sense"])


class RelatedDoc(BaseModel):
    doc_id: str
    title: str
    summary: str
    score: float = Field(ge=0.0, le=1.0)
    uri: Optional[str] = None


class TopHubSignal(BaseModel):
    request_id: str
    timestamp: str
    model_version: str = "1.0"
    top_hub: str
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)
    related_docs: List[RelatedDoc]
    proposed_actions: List[str]
    provenance: dict


@router.get("/top-hub-signal", response_model=TopHubSignal)
async def top_hub_signal() -> TopHubSignal:
    request_id = str(uuid4())
    started = datetime.now(timezone.utc)

    try:
        signal = sense_top_hub_signal()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sense failed: {exc}") from exc

    elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000

    # Structured log for auditability
    # (use your preferred logger; example with print-style for clarity)
    print(
        "top_hub_signal",
        {
            "request_id": request_id,
            "top_hub": signal["top_hub"],
            "confidence": signal["confidence"],
            "elapsed_ms": round(elapsed_ms, 1),
        },
    )

    return TopHubSignal(
        request_id=request_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        top_hub=signal["top_hub"],
        rationale=signal["rationale"],
        confidence=signal["confidence"],
        related_docs=[
            RelatedDoc(
                doc_id=d["doc_id"],
                title=d["title"],
                summary=d["summary"],
                score=d["score"],
                uri=d.get("uri"),
            )
            for d in signal["related_docs"]
        ],
        proposed_actions=signal["proposed_actions"],
        provenance=signal["provenance"],
    )
```

`costinel/sense/top_hub.py`
```python
from typing import Dict, List

# Lightweight adapter to knowledge-rag / graph layer.
# Replace imports/calls below with your actual RAG/Graph SDK or local queries.
# This keeps the endpoint read-only and side-effect-free.

def _query_knowledge_rag_top_hub(limit_related: int = 7) -> Dict:
    """
    Query knowledge-rag for the most-connected hub and related docs.
    Expected to return:
      {
        "top_hub": str,
        "rationale": str,
        "confidence": float,
        "related_docs": [
          {"doc_id": str, "title": str, "summary": str, "score": float, "uri": str|None},
          ...
        ],
        "proposed_actions": [str, ...],
        "provenance": {...}
      }
    """
    # Placeholder integration — wire this to your actual RAG/graph system.
    # Examples:
    # - call internal SDK: graph.top_hubs(limit=1)
    # - run local Cypher/Gremlin query for highest-degree node
    # - invoke RAG retriever for top docs by hub
    #
    # For immediate shipping, return a deterministic stub that can be replaced
    # with real integration in follow-ups.

    return {
        "top_hub": "MOC",
        "rationale": "MOC (Map of Costs) is the most-connected hub in the knowledge graph, "
                     "linking cloud cost anomalies, tagging strategies, and governance policies.",
        "confidence": 0.87,
        "related_docs": [
            {
                "doc_id": "cost-anomaly-detection-v2",
                "title": "Cost Anomaly Detection Patterns",
                "summary": "Patterns for detecting spend spikes and idle resources across multi-cloud.",
                "score": 0.94,
                "uri": "docs://sense/cost-anomaly-detection-v2",
            },
            {
                "doc_id": "tagging-governance-playbook",
                "title": "Tagging Governance Playbook",
                "summary": "Required tags, enforcement policies, and allocation models.",
                "score": 0.91,
                "uri": "docs://govern/tagging-playbook",
            },
            {
                "doc_id": "ri-coverage-analysis",
                "title": "RI Coverage Analysis Methods",
                "summary": "How to compute RI/SP coverage and savings opportunities.",
                "score": 0.88,
                "uri": None,
            },
            {
                "doc_id": "forecasting-accuracy",
                "title": "Forecasting Accuracy Guidelines",
                "summary": "Evaluation metrics and model choices for cost forecasts.",
                "score": 0.82,
                "uri": None,
            },
            {
                "doc_id": "change-management-handoff",
                "title": "Change Management Handoff",
                "summary": "Process to convert signals into tracked change requests.",
                "score": 0.79,
                "uri": None,
            },
        ],
        "proposed_actions": [
            "Review top cost anomalies linked to MOC this week.",
            "Validate tagging coverage for resources in high-spend accounts.",
            "Run RI coverage analysis for the top 3 services.",
            "Create a proposal for governance policy update based on detected patterns.",
        ],
        "provenance": {
            "source": "knowledge-rag",
            "graph_snapshot": "latest",
            "retrieved_at": "runtime",
        },
    }


def sense_top_hub_signal() -> Dict:
    """
    Sense top-hub signal (read-only). No side effects.
    """
    return _query_knowledge_rag_top_hub(limit_related=7)
```

`costinel/api/__init__.py` (ensure router is included)
```python
from fastapi import FastAPI
from costinel.api.v1.sense.top_hub_signal import router as top_hub_router

def register_api_routes(app: FastAPI) -> None:
    app.include_router(top_hub_router)
```

### Acceptance checks (quick)
- `GET /api/v1/sense/top-hub-signal` returns 200 and matches `TopHubSignal` schema.
- Response includes `top_hub`, `rationale`, `confidence`, `related_docs`, `proposed_actions`, and `provenance`.
- No writes to cloud resources or local database (read-only).
- RequestId and timestamp present for auditability.

