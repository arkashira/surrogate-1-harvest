# airship / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

I merged both proposals, kept the strongest technical choices, removed contradictions, and prioritized concrete, copy-paste-ready implementation.

### 1. Diagnosis (merged, de-duplicated)
- No discovery entrypoint or healthcheck for Arkship (8000) and Surrogate (8001).
- Top-hub insight (most-connected hubs) exists in Neo4j but is not operationalized.
- Training file listing recomputes each run → risks HF API 429; needs CDN-ready, cached file list.
- No lightweight knowledge-RAG query path after market-analysis runs.
- Missing local CLI + service inventory for onboarding/debugging.

### 2. Proposed change (merged + corrected)
- Add **discovery micro-helper** to Surrogate:
  - FastAPI routes under `/discovery/` (health, top-hubs, training-files).
  - Local CLI at `/opt/axentx/airship/surrogate/bin/discovery-cli` (works with or without the API).
  - Optional: add a Makefile target `make discovery` for one-liner workflows.
- Corrected choices:
  - Use **non-recursive** HF Hub `list_repo_tree` per date folder to minimize pagination/rate-limit risk.
  - Cache training file lists to disk (TTL optional) to avoid re-listing.
  - Healthcheck is **read-only + fast** (Neo4j lightweight query; Qdrant `/healthz`).
  - Keep Surrogate as the canonical place for discovery (single source of truth), but CLI can query remote or run local logic.

### 3. Implementation (final, copy-paste-ready)

#### 3.1 FastAPI discovery routes
Create `/opt/axentx/airship/surrogate/api/routes/discovery.py`:

```python
# /opt/axentx/airship/surrogate/api/routes/discovery.py
from fastapi import APIRouter, HTTPException
from datetime import datetime
import httpx
import json
import os
from typing import List, Dict, Any

router = APIRouter(prefix="/discovery", tags=["discovery"])

# -- Neo4j helpers --
def _neo4j_driver():
    uri = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    pwd = os.getenv("NEO4J_PASSWORD", "neo4j")
    from neo4j import GraphDatabase
    return GraphDatabase.driver(uri, auth=(user, pwd))

def neo4j_top_hubs(limit: int = 5) -> List[Dict[str, Any]]:
    try:
        driver = _neo4j_driver()
        with driver.session() as session:
            result = session.run(
                """
                MATCH (h:Hub)
                RETURN h.name AS name, size((h)--()) AS connections
                ORDER BY connections DESC
                LIMIT $limit
                """,
                limit=limit,
            )
            hubs = [{"name": r["name"], "connections": r["connections"]} for r in result]
        driver.close()
        return hubs
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Neo4j unavailable: {exc}")

# -- HF training file listing --
def build_training_file_list(date_str: str, repo: str = "datasets/your-org/surrogate-1") -> Dict[str, Any]:
    cache_dir = "/tmp/surrogate_discovery_cache"
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"files_{date_str}.json")

    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                return json.load(f)
        except Exception:
            os.remove(cache_path)

    try:
        from huggingface_hub import HfApi
        api = HfApi()
        # Non-recursive to minimize pagination and rate-limit risk
        files = api.list_repo_tree(repo=repo, path=date_str, recursive=False)
        file_list = [
            {
                "path": f.rfilename,
                "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{f.rfilename}",
            }
            for f in files
            if hasattr(f, "rfilename")
        ]
        payload = {
            "date": date_str,
            "repo": repo,
            "files": file_list,
            "cached_at": datetime.utcnow().isoformat(),
        }
        with open(cache_path, "w") as f:
            json.dump(payload, f)
        return payload
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HF listing failed: {exc}")

# -- Routes --
@router.get("/health")
def health() -> Dict[str, str]:
    """Lightweight readiness for Arkship orchestration."""
    checks = {
        "surrogate_api": "ok",
        "timestamp": datetime.utcnow().isoformat(),
    }

    # Neo4j liveness (lightweight)
    try:
        driver = _neo4j_driver()
        with driver.session() as session:
            session.run("RETURN 1")
        driver.close()
        checks["neo4j"] = "ok"
    except Exception:
        checks["neo4j"] = "unavailable"

    # Qdrant liveness
    try:
        qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
        resp = httpx.get(f"{qdrant_url}/healthz", timeout=2.0)
        checks["qdrant"] = "ok" if resp.status_code == 200 else "degraded"
    except Exception:
        checks["qdrant"] = "unavailable"

    return checks

@router.get("/top-hubs")
def top_hubs(limit: int = 5) -> List[Dict[str, Any]]:
    """Most-connected hubs for discovery/planning (pattern: top-hub doc insight)."""
    return neo4j_top_hubs(limit=limit)

@router.get("/training-files")
def training_files(date: str, repo: str = "datasets/your-org/surrogate-1") -> Dict[str, Any]:
    """
    CDN-ready file list for Surrogate-1 training.
    Expected date format: YYYY-MM-DD
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    return build_training_file_list(date_str=date, repo=repo)

@router.get("/knowledge-rag/top-context")
def knowledge_rag_top_context(hub_name: str, limit: int = 5) -> Dict[str, Any]:
    """
    Quick knowledge-RAG context fetch for a hub (e.g., after market-analysis).
    Returns short summaries/metadata for top related docs in vector store.
    """
    try:
        qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
        collection = os.getenv("QDRANT_COLLECTION", "knowledge")
        # Lightweight search for top docs related to hub
        resp = httpx.post(
            f"{qdrant_url}/collections/{collection}/points/search",
            json={
                "vector": {"hub": hub_name},
                "limit": limit,
                "with_payload": True,
                "with_vector": False,
            },
            timeout=5.0,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Qdrant search failed")
        data = resp.json()
        return {"hub": hub_name, "results": data.get("result", [])}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"RAG context fetch failed: {exc}")
```

#### 3.2 Wire into main FastAPI app
Edit `/opt/axentx/airship/surrogate/api/main.py`:

```python
# snippet to include in main.py
from fastapi import FastAPI
from api.routes import discovery  # <-- ensure this import exists

app = FastAPI(title="Surrogate AI")
app.include_router(discovery.router)
# ... existing includes below
```

#### 3.3 Local CLI helper
Create `/opt/axentx/
