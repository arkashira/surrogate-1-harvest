# Costinel / backend

## Implementation Plan — Costinel Top-Hub Signal (Backend)

**Scope:** Highest-value, read-only, <2h  
**Endpoint:** `GET /api/v1/cost-anomaly/signal/top-hub`  
**Guiding principle:** “Sense + Signal — ไม่ Execute” (no side effects, no mutations).

---

### 1. Architecture & Data Flow

```
Client
  │
  ▼
FastAPI (Costinel Backend)
  │
  ├─> KnowledgeRAGService (reuse existing RAG pipeline)
  │      ├─> list_repo_tree (single date folder) → cache to JSON
  │      └─> CDN-only fetches (no HF API during inference)
  │
  ├─> TopHubGraphService (graph centrality)
  │      └─> compute hub scores (degree + pagerank)
  │
  └─> SignalComposer
         └─> { hub, score, context, anomalies[], recommendations[] }
```

- **No mutations** — pure read path.
- **Reuse** existing RAG + graph modules (pattern: top-hub doc insight).
- **Rate-limit safe** — pre-list file paths once, embed in service; training/inference uses CDN URLs only.

---

### 2. File Changes

#### `costinel/backend/api/v1/endpoints/cost_anomaly.py`
```python
from fastapi import APIRouter, Depends, HTTPException
from costinel.services.signal import TopHubSignalService
from costinel.schemas.signal import TopHubSignalResponse

router = APIRouter(prefix="/cost-anomaly", tags=["cost-anomaly"])

@router.get("/signal/top-hub", response_model=TopHubSignalResponse)
async def get_top_hub_signal(
    *,
    days: int = 7,
    service: TopHubSignalService = Depends(),
) -> TopHubSignalResponse:
    """
    Sense + Signal: return top-hub insight with context and anomalies.
    No execution, no mutations.
    """
    try:
        signal = await service.compose(days=days)
        return signal
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail="signal composition failed")
```

#### `costinel/services/signal.py`
```python
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from costinel.config import settings
from costinel.schemas.signal import TopHubSignalResponse, HubInsight, AnomalySummary

HF_DATASET = settings.hf_dataset_repo  # e.g. "axentx/costinel-knowledge"
DATE_FMT = "%Y-%m-%d"

class TopHubSignalService:
    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.http = http_client or httpx.AsyncClient(timeout=30.0)
        self.cache_dir = Path(cache_dir or Path("/tmp/costinel_hub_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def compose(self, *, days: int = 7) -> TopHubSignalResponse:
        since = datetime.now(UTC) - timedelta(days=days)

        # 1) Fetch top-hub from graph (reuse existing RAG + centrality)
        hub = await self._resolve_top_hub(since=since)

        # 2) Pull recent anomalies & cost signals for hub
        anomalies = await self._fetch_anomalies(hub=hub, since=since)

        # 3) Compose signal
        return TopHubSignalResponse(
            hub=hub,
            anomalies=anomalies,
            generated_at=datetime.now(UTC),
            window_days=days,
            guidance="Sense + Signal — ไม่ Execute",
        )

    async def _resolve_top_hub(self, since: datetime) -> HubInsight:
        """
        Reuse existing graph centrality (degree + pagerank).
        If unavailable, fallback to most-connected node from file-tree.
        """
        try:
            from costinel.services.graph import TopHubGraphService
            graph_svc = TopHubGraphService()
            node = await graph_svc.top_hub(since=since)
            return HubInsight(
                id=node.id,
                name=node.name,
                type=node.type,
                score=node.score,
                context=node.context or "",
            )
        except Exception:
            logger.warning("graph service unavailable, using file-tree fallback")
            return await self._fallback_top_hub_from_tree(since=since)

    async def _fallback_top_hub_from_tree(self, since: datetime) -> HubInsight:
        """
        Pattern: pre-list file paths once, embed in service.
        CDN-only fetches for content.
        """
        folder = since.strftime(DATE_FMT)
        tree_url = f"https://huggingface.co/datasets/{HF_DATASET}/tree/{folder}?recursive=False"
        resp = await self._get_json(tree_url)

        # crude heuristic: pick most-referenced filename stem as hub
        counts: dict[str, int] = {}
        for entry in resp:
            if entry.get("type") == "file":
                name = Path(entry["path"]).stem
                counts[name] = counts.get(name, 0) + 1

        if not counts:
            raise ValueError("no files found for hub resolution")

        top_name = max(counts, key=counts.get)
        # fetch context via CDN (single small file)
        context_url = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main/{folder}/{top_name}.md"
        context_resp = await self.http.get(context_url)
        context = context_resp.text[:512] if context_resp.is_success else ""

        return HubInsight(
            id=f"hub:{top_name}",
            name=top_name,
            type="module",
            score=float(counts[top_name]),
            context=context,
        )

    async def _fetch_anomalies(self, hub: HubInsight, since: datetime) -> list[AnomalySummary]:
        """
        Read-only: query cost-anomaly store (or parquet) for hub-related anomalies.
        """
        # Placeholder: integrate with existing anomaly store.
        # Keep read-only and fast (<100ms).
        return [
            AnomalySummary(
                id="anom-001",
                title=f"Spike in {hub.name} egress",
                severity="high",
                impact_usd=1240.50,
                detected_at=datetime.now(UTC),
            )
        ]

    async def _get_json(self, url: str) -> Any:
        cache_key = f"{url.replace('/', '_')}.json"
        cache_file = self.cache_dir / cache_key
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text())
            except Exception:
                pass

        resp = await self.http.get(url)
        resp.raise_for_status()
        data = resp.json()
        cache_file.write_text(json.dumps(data))
        return data
```

#### `costinel/schemas/signal.py`
```python
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

class HubInsight(BaseModel):
    id: str
    name: str
    type: str  # e.g. service, module, account
    score: float
    context: Optional[str] = None

class AnomalySummary(BaseModel):
    id: str
    title: str
    severity: str  # low|medium|high|critical
    impact_usd: float
    detected_at: datetime

class TopHubSignalResponse(BaseModel):
    hub: HubInsight
    anomalies: List[AnomalySummary]
    generated_at: datetime
    window_days: int
    guidance: str = "Sense + Signal — ไม่ Execute"
```

#### `costinel/config.py` (add if missing)
```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    hf_dataset_repo: str = "axentx/costinel-knowledge"
    hf_token: str | None = None

    class Config:
        env_file = ".env"

settings = Settings()
```

---
