# Costinel / backend

## Final Implementation Plan — Costinel Top-Hub Signal Endpoint

**Highest-value incremental improvement (<2h):**  
Expose a **read-only, side-effect-free** `GET /api/v1/cost-anomaly/signal/top-hub` that returns the most-connected hub (e.g., `"MOC"`) with a normalized score, connection count, and 2–3 concrete, actionable insights derived from graph heuristics and recent cost anomalies. No DB writes, no external calls, no training, no mutations.

---

### 1) API contract (final)

```
GET /api/v1/cost-anomaly/signal/top-hub
Response 200:
{
  "hub": "MOC",
  "score": 0.94,
  "connections": 27,
  "insights": [
    "High cross-account egress via MOC",
    "RI coverage gap in us-east-1 linked to MOC workloads",
    "3 cost anomalies in last 7d tied to MOC-linked accounts"
  ],
  "anomalies": [
    {
      "id": "anom-2026-05-01-001",
      "service": "AmazonEC2",
      "account": "123456789012",
      "impactUSD": 1240.50,
      "description": "Unattached EBS volumes in us-east-1"
    }
  ],
  "generatedAt": "2026-05-03T12:34:56Z"
}
```

- `hub`: string — most-connected hub label.  
- `score`: float [0,1] — normalized connection strength.  
- `connections`: int — number of linked entities (accounts/services).  
- `insights`: list[str] — short, actionable notes (heuristic + cost context).  
- `anomalies`: list[object] — recent cost anomalies related to the hub (last 7d).  
- `generatedAt`: ISO8601 UTC.

No request parameters. Cacheable (max-age 60s).

---

### 2) Architecture & data flow

```
Client
  │
  ▼
FastAPI: GET /api/v1/cost-anomaly/signal/top-hub
  │
  ├─► KnowledgeRAG.query_top_hub()
  │       ├─► In-memory graph: highest-degree/centrality node
  │       └─► Returns { hub, score, related_docs[] }
  │
  └─► CostAnomalyService.last_7d_by_hub(hub)
          └─► Returns [{ id, service, account, impactUSD, description }]
  │
  └─► Heuristic enrichment → insights[]
  │
  └─► Response assembled via Pydantic model
```

- **Read-only**: No DB writes, no training, no external ingestion.  
- **Fast**: In-memory graph + lightweight cost query.  
- **Safe**: No credentials or mutating actions exposed.

---

### 3) File changes (concrete)

#### A) KnowledgeRAG module
`/opt/axentx/Costinel/backend/src/knowledge_rag.py`

```python
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import List, Dict, Any
import networkx as nx
from datetime import datetime, timezone


@dataclass
class RelatedDoc:
    doc_id: str
    title: str
    relevance: float
    snippet: str


@dataclass
class TopHubResult:
    hub: str
    score: float
    related_docs: List[RelatedDoc]
    generated_at: str


class KnowledgeRAG:
    """
    Lightweight in-memory knowledge-graph accessor.
    MVP uses a deterministic synthetic graph seeded from known hubs.
    Replace _build_graph() with loader from Neo4j/pgvector/artifact in prod.
    """

    def __init__(self, graph_path: str | None = None):
        self.graph_path = graph_path
        self.G = self._build_graph()

    def _build_graph(self) -> nx.Graph:
        G = nx.Graph()
        hubs = ["MOC", "RI", "Tagging", "Budget", "Forecast", "Anomaly", "Account", "Service"]
        for h in hubs:
            G.add_node(h, kind="hub")

        # Deterministic edges to simulate connectivity
        edges = [
            ("MOC", "Account"), ("MOC", "Service"), ("MOC", "RI"),
            ("RI", "Account"), ("RI", "Service"),
            ("Tagging", "Account"), ("Tagging", "Service"),
            ("Budget", "Account"), ("Forecast", "Account"),
            ("Anomaly", "Service"), ("Anomaly", "Account"),
        ]
        for u, v in edges:
            G.add_edge(u, v)

        # Add synthetic cross-account/service edges for MOC to boost degree
        for i in range(20):
            G.add_edge("MOC", f"Account-{i}")
            G.add_edge("MOC", f"Service-{i % 5}")
        return G

    def query_top_hub(self) -> TopHubResult:
        if self.G.number_of_nodes() == 0:
            fallback = TopHubResult(
                hub="MOC",
                score=0.94,
                related_docs=[
                    RelatedDoc(
                        doc_id="doc-001",
                        title="MOC Governance Overview",
                        relevance=0.92,
                        snippet="Central governance hub for multi-account controls."
                    )
                ],
                generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            )
            return fallback

        degrees = dict(self.G.degree())
        top_node = max(degrees, key=degrees.get)
        max_deg = max(degrees.values())
        score = min(1.0, max_deg / 30.0)

        related = [
            RelatedDoc(
                doc_id=f"doc-{i}",
                title=f"{top_node} related doc {i}",
                relevance=round(0.95 - i * 0.05, 2),
                snippet=f"Relevant guidance for {top_node} governance and controls."
            )
            for i in range(3)
        ]

        return TopHubResult(
            hub=top_node,
            score=round(score, 3),
            related_docs=related,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
```

---

#### B) Cost anomaly service
`/opt/axentx/Costinel/backend/src/cost_anomaly_service.py`

```python
from __future__ import annotations

from typing import List, Dict, Any
from datetime import datetime, timedelta, timezone


class CostAnomalyService:
    """
    Lightweight accessor for recent anomalies tied to a hub.
    MVP returns deterministic synthetic anomalies keyed by hub.
    Replace with real query (e.g., Athena/QuickSight/Cost Explorer) in prod.
    """

    @staticmethod
    def last_7d_by_hub(hub: str) -> List[Dict[str, Any]]:
        # Deterministic synthetic anomalies for demo/contract stability
        synthetic_map = {
            "MOC": [
                {
                    "id": "anom-2026-05-01-001",
                    "service": "AmazonEC2",
                    "account": "123456789012",
                    "impactUSD": 1240.50,
                    "description": "Unattached EBS volumes in us-east-1"
                },
                {
                    "id": "anom-2026-05-02-004",
                    "service": "AmazonS3",
                    "account": "123456789012",
                    "impactUSD": 320.00,
                    "description": "Unexpected cross-region replication traffic"
                },
                {
                    "id": "anom-2026-05-03-002",
                    "service": "AWSTransfer",
                    "account": "987654321098",
                    "impactUSD": 75
