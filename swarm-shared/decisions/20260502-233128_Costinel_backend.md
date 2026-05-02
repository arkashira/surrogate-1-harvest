# Costinel / backend

## Final Implementation Plan — Costinel Top-Hub Signal (Backend)

**Chosen scope:** Highest-value, read-only, <2h  
**Endpoint:** `GET /api/v1/cost-anomaly/signal/top-hub`  
**Guiding principle:** “Sense + Signal — ไม่ Execute” (no side effects, no mutations, no training/ingest).

---

### 1. Architecture & Data Model (merged + corrected)

- **Endpoint:** `GET /api/v1/cost-anomaly/signal/top-hub`
- **Query params (all optional):**
  - `window`: `7d` | `30d` | `90d` (default `7d`)
  - `severity`: `low` | `medium` | `high` | `critical` (default `medium`)
  - `limit`: `1..50` (default `10`)
- **Response semantics:**
  - Lightweight signal payload for dashboards/alerts.
  - Deterministic, idempotent, read-only.
  - Uses existing patterns: `#knowledge-rag #graph #hub #top-hub`.

**Concrete response model (Pydantic):**

```python
# app/models/signal.py
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime


class RelatedDoc(BaseModel):
    doc_id: str = Field(..., description="Document/node identifier")
    title: Optional[str] = None
    snippet: Optional[str] = None
    score: float = Field(..., ge=0.0, description="Relevance / connection weight")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SignalItem(BaseModel):
    signal_id: str = Field(..., description="Unique signal identifier")
    type: str = Field(..., description="Signal type, e.g. cost-spike")
    resource: Optional[str] = None
    service: Optional[str] = None
    region: Optional[str] = None
    account_id: Optional[str] = None
    current_spend: Optional[float] = None
    baseline_spend: Optional[float] = None
    severity: str = Field(..., description="Mapped severity for this signal")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TopHubSignal(BaseModel):
    meta: Dict[str, Any] = Field(
        default_factory=lambda: {"window": "7d", "generatedAt": datetime.utcnow().isoformat() + "Z"}
    )
    top_hub: Dict[str, Any] = Field(
        ...,
        description="Top hub with anomaly context and related signals/docs",
    )
    related_docs: List[RelatedDoc] = Field(default_factory=list)
    signals: List[SignalItem] = Field(default_factory=list)
    context: Dict[str, Any] = Field(
        default_factory=lambda: {
            "pattern": "top-hub doc insight",
            "tags": ["knowledge-rag", "graph", "hub"],
            "read_only": True,
        }
    )
```

Notes on correctness:
- `top_hub` is a dict (not a rigid submodel) to allow flexible hub schemas from the graph while keeping response stable.
- `signals` is a list of concrete signal items (cost-spike style) for dashboard/alert usability.
- `related_docs` preserves the RAG/graph insight linkage.
- `meta.generatedAt` uses UTC ISO-8601.

---

### 2. File Layout (existing repo assumed)

```
/opt/axentx/Costinel/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── api/
│   │   ├── v1/
│   │   │   ├── __init__.py
│   │   │   ├── cost_anomaly.py   # <-- add route here
│   ├── core/
│   │   ├── config.py
│   │   ├── logger.py
│   ├── services/
│   │   ├── knowledge_rag.py      # <-- new/extended read-only service
│   ├── models/
│   │   ├── signal.py             # <-- Pydantic models above
│   ├── utils/
│   │   ├── graph.py              # optional helpers
```

---

### 3. Knowledge-RAG Service (read-only)

`app/services/knowledge_rag.py`

```python
import time
from typing import List, Dict, Any, Optional
from app.models.signal import RelatedDoc, SignalItem


class KnowledgeRAG:
    """
    Read-only interface to knowledge-rag graph.
    Replace internals with your actual graph client (Neo4j / NetworkX / custom).
    """

    def __init__(self, graph_client=None):
        self.graph = graph_client  # inject or init lazily

    # ----------------------------
    # Graph query helpers (stubs)
    # ----------------------------
    def _find_top_hub_node(self) -> Dict[str, Any]:
        """
        Find the most-connected hub node.
        Replace with real query, e.g. Neo4j:
          MATCH (h:Hub)
          WITH h, size((h)--()) AS deg
          ORDER BY deg DESC LIMIT 1
          RETURN h.id, h.label, h.type, deg AS hub_score
        """
        return {
            "hub_id": "MOC",
            "hub_label": "MOC",
            "hub_type": "management-account",
            "hub_score": 42.0,
            "display_name": "MOC — Management Operations Center",
        }

    def _find_related_docs(self, hub_id: str, top_k: int = 5) -> List[RelatedDoc]:
        """
        Find top related docs by connection strength.
        Replace with real query, e.g.:
          MATCH (h {id:$hub_id})-[r]-(doc)
          RETURN doc.id, doc.title, doc.snippet, r.weight
          ORDER BY r.weight DESC LIMIT $top_k
        """
        return [
            RelatedDoc(
                doc_id=f"doc_{i}",
                title=f"Related doc {i}",
                snippet="Summary or excerpt from knowledge-rag.",
                score=round(10.0 / (i + 1), 3),
                metadata={"source": "knowledge-rag", "type": "insight"},
            )
            for i in range(1, top_k + 1)
        ]

    def _find_signals_for_hub(self, hub_id: str, limit: int = 10, severity: str = "medium") -> List[SignalItem]:
        """
        Find top signals related to the hub (cost anomalies, spikes, etc.).
        Replace with real query against signals/anomalies index.
        """
        # Emulated deterministic signals
        sample = [
            SignalItem(
                signal_id=f"cost-spike-ec2-{hub_id.lower()}-prod-2026050{i}",
                type="cost-spike",
                resource=f"i-0a1b2c3d4e5f6789{i}",
                service="EC2",
                region="ap-southeast-1",
                account_id="123456789012",
                current_spend=1240.0 + (i * 100),
                baseline_spend=400.0,
                severity=severity,
                metadata={"window": "7d", "anomaly_score": round(0.7 + (i * 0.05), 2)},
            )
            for i in range(1, min(limit, 5) + 1)
        ]
        return sample[:limit]

    # ----------------------------
    # Public read-only API
    # ----------------------------
    def get_top_hub_signal(self, window: str = "7d", severity: str = "medium", limit: int = 10, top_k_docs: int = 5) -> Dict[str, Any]:
        hub = self._find_top_hub_node()
        related_docs = self._find_related_docs(hub_id=hub["hub_id"], top_k=top_k_docs)
        signals = self._find_signals_for_hub(hub_id=hub["hub_id"], limit=limit, severity=severity)

        top_hub_payload = {
            "hub_id": hub["hub_id"],
            "hub_type": hub["hub_type"],
            "display_name": hub.get("display_name", hub["hub_label"]),
            "anomaly_score": round(min(1.0, hub["hub
