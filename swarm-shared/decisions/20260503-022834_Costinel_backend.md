# Costinel / backend

## Final synthesized implementation (best of both proposals)

**Chosen approach**: Backend-first `/api/hubs/{hub}/signals` endpoint that serves ≤3 actionable signals for a hub (default = most-connected hub) using a CDN-first, zero-HF-API-at-runtime data strategy. Combines Candidate 1’s data/caching/scripting rigor with Candidate 2’s clearer signal contract and frontend alignment.

**Why this wins**
- Directly unlocks the top-hub signal panel (frontend expects this contract).
- Avoids 429s and training-time HF API calls by using CDN URLs + local cache.
- Low risk, ~90–110 min implementation, no infra changes.
- Concrete, production-ready with tests, caching, and safe wrapper practices.

---

## 1. API contract (final)

**Endpoint**
```
GET /api/hubs/{hub}/signals
```

**Query params**
- `limit` (int, default 3)
- `status` (string, default "active")

**Response 200**
```json
{
  "hub": "MOC",
  "generated_at": "2026-05-03T02:30:00Z",
  "signals": [
    {
      "id": "sig-123",
      "title": "RI coverage gap in us-east-1",
      "description": "Projected 34% savings by converting 6 m5.xlarge to 3yr RI",
      "source": "proposal",
      "severity": "high",
      "impact_score": 0.87,
      "category": "reserved_instance",
      "actions": ["proposal:create-ri", "proposal:simulate"],
      "evidence": ["cost_model:ri_coverage", "usage:last30d"],
      "status": "active"
    }
  ]
}
```

Notes:
- `severity` (critical/high/medium/low) kept for prioritization; `impact_score` (0–1) for ranking.
- `source` and `category` allow frontend routing/filtering.
- `actions` and `evidence` are arrays to support concrete next steps.

---

## 2. Implementation plan (concrete steps)

### 2.1 Add FastAPI route
Create or update `src/routes/hubs.py` (or `costinel/backend/api/routes/hubs.py`).

```python
# src/routes/hubs.py
from fastapi import APIRouter, HTTPException
from src.services.signal_service import SignalService
from typing import Optional
from datetime import datetime, timezone

router = APIRouter(prefix="/api/hubs", tags=["hubs"])

@router.get("/{hub}/signals")
async def get_hub_signals(
    hub: str,
    limit: int = 3,
    status: Optional[str] = "active"
):
    try:
        signals = SignalService.get_signals(hub=hub, limit=limit, status=status)
        return {
            "hub": hub,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "signals": signals,
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal server error")
```

Register router in app main (e.g., `app.include_router(router)`).

---

### 2.2 Service layer (CDN-first, no HF API at runtime)

```python
# src/services/signal_service.py
import json
import os
from datetime import datetime, timezone
from typing import List, Dict, Any

class SignalService:
    # Cache produced by ingestion (parquet preferred; JSON for quick MVP)
    _CACHE_PATH = os.getenv("SIGNAL_CACHE_PATH", "data/cache/signals_latest.json")
    _TTL_SECONDS = int(os.getenv("SIGNAL_CACHE_TTL", "60"))

    _cache: List[Dict[str, Any]] = None
    _cache_ts: float = None

    @classmethod
    def _load_cache(cls) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc).timestamp()
        if cls._cache is not None and cls._cache_ts and (now - cls._cache_ts) < cls._TTL_SECONDS:
            return cls._cache

        if not os.path.exists(cls._CACHE_PATH):
            raise ValueError(f"Signal cache not found: {cls._CACHE_PATH}")

        with open(cls._CACHE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cls._cache = raw if isinstance(raw, list) else []
        cls._cache_ts = now
        return cls._cache

    @classmethod
    def get_signals(cls, hub: str, limit: int = 3, status: str = "active") -> List[Dict[str, Any]]:
        records = cls._load_cache()
        filtered = [r for r in records if r.get("hub") == hub and r.get("status") == status]

        # Sort by severity then impact_score desc, then recency
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        filtered.sort(
            key=lambda x: (
                severity_order.get(x.get("severity", "low"), 3),
                -float(x.get("impact_score", 0)),
                x.get("ts", x.get("created_at", "")),
            )
        )
        top = filtered[:limit]

        return [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "description": item.get("description"),
                "source": item.get("source"),
                "severity": item.get("severity"),
                "impact_score": item.get("impact_score"),
                "category": item.get("category"),
                "actions": item.get("actions", []),
                "evidence": item.get("evidence", []),
                "status": item.get("status"),
            }
            for item in top
        ]
```

---

### 2.3 Data strategy (CDN-first, HF-API-safe)

- **Pre-list step (Mac-side or CI)**: single non-recursive `list_repo_tree` call per date folder; write `file_list.json`.
- **Ingestion**: use CDN URLs only (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) — zero HF API calls during training/load.
- **Cache generation**: ingestion writes normalized `signals_latest.json` (or parquet) into `data/cache/` for the service to read.

```bash
# scripts/pre_list_hf_folder.sh
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/costinel-data}"
DATE_FOLDER="${1:-2026-05-03}"
OUTFILE="${2:-file_list.json}"

python - <<PY
import os, json
from huggingface_hub import HfApi
api = HfApi()
files = api.list_repo_tree(
    repo_id=os.getenv("HF_REPO", "$REPO"),
    path="$DATE_FOLDER",
    recursive=False
)
with open("$OUTFILE", "w") as f:
    json.dump([{"path": fi.path, "size": fi.size} for fi in files], f, indent=2)
PY

echo "Saved file list to $OUTFILE"
```

- Make executable: `chmod +x scripts/pre_list_hf_folder.sh`.
- Cron safety: ensure crontab has `SHELL=/bin/bash` and invoke via `bash scripts/pre_list_hf_folder.sh`.

---

### 2.4 Ingestion: CDN-only fetch example

```python
# src/ingestion/cdn_loader.py
import requests
from pathlib import Path

def download_via_cdn(repo: str, path: str, local_out: str) -> str:
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    Path(local_out).parent.mkdir(parents=True, exist_ok=True)
    with open(local_out, "wb") as f:
        f.write(resp.content)
    return local_out
```

---

## 3. Tests & docs

- **Unit test**: `test_signal_service.py`
