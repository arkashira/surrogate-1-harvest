# Costinel / backend

**Final Synthesized Design — Costinel Top-Hub Signal (Backend)**

**Guiding principle (non-negotiable):**  
Sense + Signal — **no side effects, no mutations, read-only**.

---

## 1. Architecture (production-grade, minimal viable scope)

- **API layer**: FastAPI (async, automatic OpenAPI, type safety) — *preferred over Flask for performance and developer experience*.  
- **Compute layer**: Lightweight service (stateless) that can run in existing Lightning Studio or any container.  
- **Data source**: Hugging Face Hub repository tree (or internal dataset-mirror) — *no PostgreSQL needed for read-only signal*.  
- **Signal logic**: Deterministic graph centrality (NetworkX) **or** a small PyTorch scorer — *choose one based on actual signal definition*.  
- **Deployment**: Single Docker container, Kubernetes Deployment (1+ replicas), resource-constrained.

---

## 2. Data ingestion & processing (read-only)

- **Ingestion**: Use `huggingface_hub` (`list_repo_tree`) to fetch repo/file tree.  
- **Projection**: Keep only `{path, size, commit_time}` (or `{prompt, response}` if downstream requires).  
- **Storage**: In-memory graph built per request (or cached for ≤ N seconds) — avoids DB for read-only endpoint.  
- **Validation**: Reject non-200 upstream responses; fail fast with 502 if source unavailable.

---

## 3. Signal calculation (two concrete options — pick one)

**Option A — Graph centrality (deterministic, explainable)**  
- Build directed graph from repo/file dependencies or access patterns.  
- Top-hub = node with highest betweenness centrality (or `nx.center` for radius-based).  
- Return: `{ "hub": "path/to/repo_or_file", "score": 0.0–1.0 }`.

**Option B — Learned scorer (if historical labels exist)**  
- Small PyTorch model (e.g., 128→64→1) trained to predict anomaly likelihood.  
- Input: fixed-size features derived from repo/file metadata.  
- Return: `{ "signal": float, "hub": "path/to/item" }`.

**Concrete choice for implementation below:** Use **Option A** (no training data requirement, fully deterministic, matches “Sense + Signal”).

---

## 4. API endpoint (FastAPI)

```python
# main.py
from fastapi import FastAPI, HTTPException
from huggingface_hub import HfApi
import networkx as nx
from typing import Dict, Any
import time

app = FastAPI(title="Costinel Top-Hub Signal", docs_url="/docs")

HF_API = HfApi()
REPO_ID = "your-org/top-hub"  # configure via env
CACHE_TTL_SECONDS = 30
_last_result = None
_last_ts = 0

def _build_graph_from_tree(tree):
    g = nx.DiGraph()
    for node in tree:
        path = node.path
        g.add_node(path, type=node.type, size=node.size if node.size else 0)
        # simple parent->child edges for directory structure
        parts = path.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            child = "/".join(parts[: i + 1])
            g.add_edge(parent, child)
    return g

def _compute_top_hub() -> Dict[str, Any]:
    try:
        tree = HF_API.list_repo_tree(REPO_ID, recursive=True)
    except Exception as exc:
        raise RuntimeError(f"HuggingFace fetch failed: {exc}") from exc

    g = _build_graph_from_tree(tree)
    if len(g) == 0:
        return {"hub": None, "score": 0.0}

    try:
        centrality = nx.betweenness_centrality(g)
    except Exception:
        # fallback: use degree
        centrality = dict(g.degree())
        max_deg = max(centrality.values()) if centrality else 1
        centrality = {k: (v / max_deg) for k, v in centrality.items()}

    hub = max(centrality, key=centrality.get)
    score = float(centrality[hub])
    return {"hub": hub, "score": score}

@app.get("/api/v1/cost-anomaly/signal/top-hub")
def top_hub_signal() -> Dict[str, Any]:
    global _last_result, _last_ts
    now = time.time()
    if _last_result is None or (now - _last_ts) > CACHE_TTL_SECONDS:
        try:
            _last_result = _compute_top_hub()
            _last_ts = now
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
    return {"signal": _last_result["score"], "hub": _last_result["hub"]}
```

**Why FastAPI over Flask:**  
- Async-ready, automatic validation/docs, lower overhead for I/O-bound fetch.  
- Keeps code small and deployable in existing Lightning Studio if needed.

---

## 5. Testing (concrete, minimal)

- **Unit tests**: Mock `HfApi.list_repo_tree` and verify centrality selection.  
- **Integration test**: Spin up TestClient, assert 200 and schema `{signal: float, hub: str|null}`.  
- **Fail-fast tests**: Simulate upstream failure → expect 502.

Example (pytest):
```python
from fastapi.testclient import TestClient
from unittest.mock import patch
from main import app

client = TestClient(app)

def test_top_hub():
    with patch("main.HF_API.list_repo_tree") as mock_tree:
        mock_tree.return_value = [
            type("Node", (), {"path": "a", "type": "file", "size": 100}),
            type("Node", (), {"path": "a/b", "type": "file", "size": 50}),
        ]
        r = client.get("/api/v1/cost-anomaly/signal/top-hub")
        assert r.status_code == 200
        assert "signal" in r.json()
        assert "hub" in r.json()
```

---

## 6. Deployment (Docker + K8s)

**Dockerfile** (slim, non-root):
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
USER 1000
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**requirements.txt**
```
fastapi
uvicorn[standard]
huggingface_hub
networkx
```

**Kubernetes Deployment (minimal)**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: costinel-top-hub
spec:
  replicas: 2
  selector:
    matchLabels:
      app: costinel-top-hub
  template:
    metadata:
      labels:
        app: costinel-top-hub
    spec:
      containers:
        - name: api
          image: your-registry/costinel-top-hub:latest
          ports:
            - containerPort: 8000
          resources:
            limits:
              cpu: "500m"
              memory: "256Mi"
          env:
            - name: REPO_ID
              value: "your-org/top-hub"
---
apiVersion: v1
kind: Service
metadata:
  name: costinel-top-hub
spec:
  selector:
    app: costinel-top-hub
  ports:
    - port: 80
      targetPort: 8000
```

---

## 7. Resolved contradictions (correctness + actionability)

| Contradiction | Resolution |
|--------------|------------|
| Flask vs FastAPI vs Lightning | Choose **FastAPI** for performance, validation, and docs; can still run in Lightning Studio if needed. |
| NetworkX vs PyTorch signal | Use **deterministic graph centrality** (no training data required). Switch to PyTorch scorer only if labeled anomaly data exists. |
| PostgreSQL vs in-memory | Endpoint is **read-only and stateless**; avoid DB complexity. Add Redis cache if TTL-based caching needed at scale. |
| Heavy deployment vs simple |
