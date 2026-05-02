# Costinel / quality

Candidate 3:
### Highest-Value Incremental Improvement (<2h)
**Improvement:** Add a "Top Hub" section to the `GET /api/v1/cost-anomaly/signal` endpoint by querying the knowledge graph for the strongest cost-anomaly signal.

### Implementation Plan
#### Step 1: Update Endpoint to Include Top Hub Section
Modify the `GET /api/v1/cost-anomaly/signal` endpoint to include a new section, "Top Hub", which will display the most-connected hub related to the strongest cost-anomaly signal.

```python
# api/v1/cost_anomaly.py
from flask import jsonify
from knowledge_rag import get_top_hub

def get_cost_anomaly_signal():
    # Existing code to retrieve cost-anomaly signal
    signal = ...

    # Get top hub related to the strongest cost-anomaly signal
    top_hub = get_top_hub(signal)

    # Return the cost-anomaly signal with the top hub section
    return jsonify({
        'signal': signal,
        'top_hub': top_hub
    })
```

#### Step 2: Implement Knowledge-Rag Pipeline
Create a new function, `get_top_hub`, which will query the knowledge-rag pipeline to retrieve the top hub related to the strongest cost-anomaly signal.

```python
# knowledge_rag.py
import networkx as nx

def get_top_hub(signal):
    # Load the knowledge graph
    G = nx.read_gpickle('knowledge_graph.gp')

    # Query the graph to retrieve the top hub related to the signal
    top_hub = None
    max_degree = 0
    for node in G.nodes():
        if G.nodes[node]['signal'] == signal:
            degree = G.degree(node)
            if degree > max_dcregree:
                max_degree = degree
                top_hub = node

    return top_hub
```

#### Step 3: Update Frontend to Display Top Hub Section
Modify the frontend to display the "Top Hub" section, which will render the top hub related to the strongest cost-anomaly signal.

```html
<!-- frontend/templates/cost_anomaly.html -->
<div>
    <h2>Today's Strongest Cost-Anomaly Signal</h2>
    <p>Signal: {{ signal }}</p>
    <p>Top Hub: {{ top_hub }}</p>
</div>
```

### Code Snippets
* `api/v1/cost_anomaly.py`: Modified to include the "Top Hub" section
* `knowledge_rag.py`: New function, `get_top_hub`, to query the knowledge-rag pipeline
* `frontend/templates/cost_anomaly.html`: Modified to display the "Top Hub" section

### Example Use Case
1. User navigates to the Costinel frontend
2. The `GET /api/v1/cost-anomaly/signal` endpoint is called to retrieve the strongest cost-anomaly signal
3. The endpoint returns the signal with the top hub section
4. The frontend renders the signal and top hub section

### Tags
* #cost-anomaly
* #knowledge-rag
* #top-hub
* #costinel
* #frontend
#api
#implementation-plan

---

## Final Synthesis

**Chosen approach:** Merge Candidate 2’s hardened, observable, testable API contract with Candidate 1/3’s “Top Hub” knowledge-graph enrichment.  
**Why:** Candidate 2 provides correctness, safety, and operational hygiene (strict schema, validation, observability, caching). Candidates 1/3 add concrete user value (contextual “Top Hub” from the knowledge graph). Combining them yields a deterministic, production-ready endpoint that also delivers actionable insight.

**Resolved contradictions in favor of correctness + actionability:**
- Use FastAPI with Pydantic (Candidate 2) instead of bare Flask (Candidates 1/3) for validation, OpenAPI docs, and type safety.
- Keep Candidate 2’s strict response shape and status-code policy; embed “top_hub” as an optional field so the contract remains deterministic when graph data is unavailable.
- Adopt Candidate 2’s caching, logging, metrics, and test plan; add one targeted metric for graph lookup latency.
- Fix Candidates 1/3 bugs: `max_dcregree` typo; avoid loading the graph per request (cache it); handle missing graph/data gracefully.
- Make graph enrichment non-blocking and fast: load the graph once at startup, use a read-only lookup, and fail gracefully (return null + log) rather than 5xx.

---

## Final Implementation Plan (<2h)

### 1) Endpoint contract (strict, deterministic)
`GET /api/v1/cost-anomaly/signal`

**Params (optional, validated):**
- `window` (preset: `last_24h`, `last_7d`, `last_30d`; or explicit `start`/`end` ISO8601)
- `severity_gte` (`low|medium|high|critical`)
- `service` (string filter)
- `limit` (1–100, default 5)

**Success (200) shape:**
```json
{
  "signal_id": "uuid",
  "generated_at": "ISO8601",
  "window": { "start": "ISO8601", "end": "ISO8601" },
  "top_anomaly": {
    "service": "string",
    "resource_id": "string",
    "region": "string",
    "account_id": "string",
    "metric": "string",
    "value": "number",
    "baseline": "number",
    "delta_percent": "number",
    "severity": "low|medium|high|critical",
    "description": "string"
  },
  "summary": {
    "total_anomalies": "integer",
    "max_delta_percent": "number"
  },
  "top_hub": {
    "node_id": "string",
    "name": "string",
    "type": "string",
    "degree": "integer",
    "attributes": { "string": "any" }
  } | null
}
```
- No data → `top_anomaly: null`, `top_hub: null`, `summary` zeros.
- `400` for invalid params; `503` if signal generation fails (fail-fast).

### 2) Caching & performance
- `Cache-Control: public, max-age=60, stale-while-revalidate=30`
- ETag on deterministic payload (hash of `signal_id+generated_at`)
- Knowledge graph loaded once at startup (read-only)

### 3) Observability
- Structured JSON logs per request: method, path, status, duration_ms, signal_id, severity, account_id, top_hub_node_id
- Metrics:
  - `costinel_anomaly_signal_duration_seconds` (histogram)
  - `costinel_anomaly_signal_requests_total{status}` (counter)
  - `costinel_kg_lookup_duration_seconds` (histogram)
  - `costinel_kg_lookup_errors_total` (counter)

### 4) Tests & smoke
- Unit: validation, deterministic shape, error codes, empty result shape
- Graph lookup: hit, miss, corrupt graph (graceful null)
- Smoke: `curl -i http://localhost:8000/api/v1/cost-anomaly/signal`

---

## Code Snippets

### FastAPI endpoint
```python
# app/api/v1/endpoints/cost_anomaly.py
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
import structlog

from app.core.metrics import metrics
from app.services.anomaly import generate_signal
from app.services.knowledge import get_top_hub_for_signal

router = APIRouter()
log = structlog.get_logger()

# ---- Query models ----
class WindowQuery(BaseModel):
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    preset: Optional[str] = Field(default="last_24h", pattern="^(last_24h|last_7d|last_30d)$")

    @field_validator("start", "end", mode="before")
    def no_future(cls, v):
        if v and v > datetime.now(timezone.utc):
            raise ValueError("cannot be in the future")
        return v


class SignalQuery(BaseModel):
    window: Optional[WindowQuery] = None
    severity_gte: Optional[str] = Field(default=None, pattern="^(low|medium|high|critical
