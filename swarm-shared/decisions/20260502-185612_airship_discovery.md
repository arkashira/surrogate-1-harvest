# airship / discovery

## Final synthesized answer

**Goal:** Add a minimal, immediately-usable discovery + health surface to Arkship (port 8000) that exposes top hubs, recent artifacts, Surrogate readiness, and machine-readable capabilities/intents/roles/domains — plus two small CLI utilities (top-hubs + file-list for CDN training). This unifies the strongest, non-overlapping parts of both proposals, resolves contradictions in favor of correctness and concrete actionability, and keeps scope <2h.

---

### 1. Diagnosis (resolved)

- Discovery is implicit: no programmatic way to list hubs, artifacts, or system capabilities.
- No canonical manifest or machine-readable index of intents/blueprints/roles/domains.
- Neo4j/Qdrant metadata opaque; Surrogate roles/domains exist but aren’t queryable.
- No readiness probe that checks Surrogate and basic knowledge-store reachability.
- No utility to produce `file-list.json` for CDN-based training to avoid HF API rate limits.

---

### 2. Proposed change (minimal, concrete)

Add to Arkship API (port 8000):

- `GET /discovery/top-hubs?limit=N` — top hubs by centrality (Neo4j first, Surrogate fallback, static stub last).
- `GET /discovery/recent-artifacts?limit=N` — latest enriched artifacts (Surrogate first, static stub fallback).
- `GET /capabilities` — machine-readable index of system capabilities, intents, blueprints, roles, domains.
- `GET /health/ready` — readiness probe (Surrogate + basic Neo4j reachability).
- CLI: `scripts/discovery-top-hubs.sh` — wraps API + optional `knowledge-rag` query.
- CLI: `scripts/build-file-list.sh` — lists a HuggingFace dataset date folder and writes `file-list.json` for CDN training.

Files to add/modify:
- `arkship/api/routes/discovery.py`
- `arkship/api/routes/capabilities.py`
- `arkship/api/routes/health.py`
- `arkship/api/app.py` (mount routers)
- `scripts/discovery-top-hubs.sh`
- `scripts/build-file-list.sh`

---

### 3. Implementation

#### 3.1 Discovery router

`arkship/api/routes/discovery.py`
```python
from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any
import os
import httpx
import logging

try:
    from neo4j import GraphDatabase
    NEO4J_URI = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
    NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
    NEO4J_DB = os.getenv("NEO4J_DB", "neo4j")
    _neo_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
except Exception as e:
    logging.getLogger(__name__).warning("Neo4j driver unavailable: %s", e)
    _neo_driver = None

SURROGATE_BASE = os.getenv("SURROGATE_BASE", "http://localhost:8001")
router = APIRouter(prefix="/discovery", tags=["discovery"])


@router.get("/top-hubs", response_model=List[Dict[str, Any]])
async def top_hubs(limit: int = 5):
    """
    Return top hubs by degree centrality.
    Priority: Neo4j -> Surrogate /knowledge/top-hubs -> static stub.
    """
    if _neo_driver:
        try:
            with _neo_driver.session(database=NEO4J_DB) as session:
                result = session.run(
                    """
                    MATCH (h:Hub)
                    RETURN h.name AS name, size((h)--()) AS score
                    ORDER BY score DESC
                    LIMIT $limit
                    """,
                    limit=limit,
                )
                hubs = [{"name": r["name"], "score": r["score"]} for r in result]
                if hubs:
                    return hubs
        except Exception as e:
            logging.getLogger(__name__).warning("Neo4j query failed: %s", e)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{SURROGATE_BASE}/knowledge/top-hubs", params={"limit": limit})
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logging.getLogger(__name__).warning("Surrogate fallback failed: %s", e)

    return [{"name": "MOC", "score": 100}, {"name": "Surrogate", "score": 80}]


@router.get("/recent-artifacts", response_model=List[Dict[str, Any]])
async def recent_artifacts(limit: int = 10):
    """
    Return recent enriched artifacts.
    Priority: Surrogate /knowledge/recent-artifacts -> static stub.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{SURROGATE_BASE}/knowledge/recent-artifacts", params={"limit": limit})
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logging.getLogger(__name__).warning("Could not fetch recent artifacts from Surrogate: %s", e)

    return [{"id": "artifact-1", "name": "sample-artifact", "updated_at": "2026-04-29T00:00:00Z"}]
```

#### 3.2 Capabilities/index router (machine-readable manifest)

`arkship/api/routes/capabilities.py`
```python
from fastapi import APIRouter
from typing import Dict, List, Any

router = APIRouter(tags=["capabilities"])

# Canonical manifest — keep in sync with code/config.
MANIFEST: Dict[str, Any] = {
    "system": "Arkship",
    "version": "1.0.0",
    "capabilities": [
        "knowledge-rag",
        "surrogate-training",
        "blueprint-orchestration",
        "domain-context",
        "vector-search",
        "graph-query",
    ],
    "intents": [
        "explain",
        "plan",
        "generate",
        "validate",
        "route",
        "summarize",
    ],
    "blueprints": [
        "moc-blueprint",
        "surrogate-blueprint",
        "ark-cicd-blueprint",
    ],
    "roles": [
        "analyst",
        "architect",
        "engineer",
        "operator",
        "planner",
        "validator",
    ],
    "domains": [
        "software-engineering",
        "data-engineering",
        "ml-ops",
        "security",
        "infrastructure",
        "product",
        "design",
        "testing",
        "observability",
        "governance",
        "finance",
        "compliance",
        "integration",
        "knowledge-management",
        "automation",
    ],
    "endpoints": {
        "discovery_top_hubs": "/discovery/top-hubs",
        "discovery_recent_artifacts": "/discovery/recent-artifacts",
        "health_ready": "/health/ready",
        "capabilities": "/capabilities",
    },
}


@router.get("/capabilities", response_model=Dict[str, Any])
async def get_capabilities():
    """
    Machine-readable index of system capabilities, intents, blueprints, roles, domains.
    """
    return MANIFEST


@router.get("/intents", response_model=List[str])
async def list_intents():
    return MANIFEST["intents"]


@router.get("/blueprints", response_model=List[str])
async def list_blueprints():
    return MANIFEST["blueprints"]


@router.get("/roles", response_model=List[str])
async def list_roles():
    return MANIFEST["roles"]


@router.get("/domains", response_model=List[str])
async def list_domains():
    return MANIFEST["domains"]
```

#### 3.3 Health/readiness probe

`arkship/api/routes/health
