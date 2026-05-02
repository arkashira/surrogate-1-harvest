# Costinel / backend

**Final Synthesis — Highest-Value, Zero-Execute “Sense + Signal” Backend Increment**

Goal (unchanged, hardened):  
Deliver a single read-only endpoint that surfaces the **top-cost anomaly for the current day** with deterministic, auditable output and **zero execution capability**. Ship in ≤2 hours by reusing existing cost data stores.

Why this wins (merged rationale):
- Strictly follows **Sense + Signal — لا Execute**.
- Minimal surface area: one endpoint + service + models + tests.
- Uses existing cost data (parquet/DB) and adds no writes.
- Provides immediate value to UI/CLI consumers.
- Concrete, production-ready patterns (FastAPI, Pydantic, dependency injection, unit tests).

---

## Implementation plan (merged, concrete, prioritized)

1. **Models** (`backend/models/anomaly.py`) — define request/response and audit metadata.
2. **Service** (`backend/services/anomaly_service.py`) — read-only anomaly detection (z-score vs 14-day trailing baseline) with deterministic selection and audit trail.
3. **Repository interface** (`backend/repositories/cost_repository.py`) — read-only protocol; implement with existing DB/parquet reader.
4. **API route** (`backend/api/anomaly.py`) — `GET /api/v1/cost/anomalies/top` returning `TopAnomalySignal`.
5. **App wiring** (`backend/main.py`) — include router and dependency override for repo.
6. **Unit test** (`tests/test_anomaly_service.py`) — deterministic service test with mocked repo.
7. **Validation** — verify endpoint is read-only, deterministic, and returns expected payload.

No DB writes. No execution hooks. No mutating operations.

---

## Code (merged best parts, hardened for correctness + actionability)

### 1) Models — `backend/models/anomaly.py`

```python
from datetime import date
from typing import Optional
from pydantic import BaseModel, Field

class AnomalyContext(BaseModel):
    baseline_mean: float = Field(..., description="Trailing 14-day mean cost")
    baseline_std: float = Field(..., description="Trailing 14-day std cost")
    current_value: float = Field(..., description="Current period cost")
    z_score: float = Field(..., description="Standardized anomaly score")
    affected_entity: str = Field(..., description="Service/account/resource id")
    entity_type: str = Field(..., description="One of: service, account, resource")

class AuditTrail(BaseModel):
    generated_at: date
    generated_by: str = Field(default="CostinelSenseEngine")
    source_table: str
    source_range: str
    method: str = Field(default="z-score vs 14-day trailing")

class TopAnomalySignal(BaseModel):
    signal_id: str = Field(..., description="Deterministic id (date-entity-z)")
    severity: str = Field(..., description="low|medium|high|critical")
    title: str
    description: str
    context: AnomalyContext
    audit: AuditTrail
    recommendation: str = Field(
        ...,
        description="Human-actionable signal (non-executable)"
    )
```

---

### 2) Service — `backend/services/anomaly_service.py`

```python
import numpy as np
from datetime import date, timedelta
from typing import Optional
from backend.models.anomaly import TopAnomalySignal, AuditTrail, AnomalyContext
from backend.models.cost import DailyCostRow  # assumed existing model

class AnomalyService:
    def __init__(self, cost_repository):
        self.repo = cost_repository

    @staticmethod
    def _z_score(value: float, mean: float, std: float) -> float:
        return (value - mean) / std if std > 0 else 0.0

    @staticmethod
    def _severity(z: float) -> str:
        az = abs(z)
        if az >= 3:
            return "critical"
        if az >= 2:
            return "high"
        if az >= 1.2:
            return "medium"
        return "low"

    def get_top_anomaly_today(self) -> Optional[TopAnomalySignal]:
        today = date.today()
        window_start = today - timedelta(days=15)
        window_end = today - timedelta(days=1)

        baseline = self.repo.get_daily_costs_between(window_start, window_end)  # list[DailyCostRow]
        today_rows = self.repo.get_daily_costs_for(today)  # list[DailyCostRow]

        if not baseline or not today_rows:
            return None

        # Build per-entity baseline stats
        series = {}
        for row in baseline:
            key = (row.entity_type, row.entity_id)
            series.setdefault(key, []).append(float(row.cost))

        best = None
        best_z = 0.0
        best_mean = 0.0
        best_std = 0.0

        for row in today_rows:
            key = (row.entity_type, row.entity_id)
            values = np.array(series.get(key, []), dtype=float)
            if len(values) < 7:
                continue
            mean = float(np.mean(values))
            std = float(np.std(values))
            z = abs(self._z_score(float(row.cost), mean, std))
            if z > best_z:
                best_z = z
                best = row
                best_mean = mean
                best_std = std

        if best is None:
            return None

        severity = self._severity(best_z)
        entity_type = best.entity_type
        entity_id = best.entity_id

        signal = TopAnomalySignal(
            signal_id=f"{today.isoformat()}-{entity_type}-{entity_id}-{best_z:.2f}",
            severity=severity,
            title=f"Cost anomaly detected on {entity_type}={entity_id}",
            description=(
                f"{entity_type} '{entity_id}' cost is {best_z:.2f}σ from its 14-day baseline "
                f"(current={best.cost:.2f}, mean={best_mean:.2f}, std={best_std:.2f})."
            ),
            context=AnomalyContext(
                baseline_mean=best_mean,
                baseline_std=best_std,
                current_value=best.cost,
                z_score=round(best_z, 2),
                affected_entity=entity_id,
                entity_type=entity_type,
            ),
            audit=AuditTrail(
                generated_at=today,
                source_table=self.repo.table_name,
                source_range=f"{window_start}..{today}",
            ),
            recommendation=(
                "Review usage patterns and consider rightsizing or commitment adjustments. "
                "No automated changes will be executed."
            ),
        )
        return signal
```

---

### 3) Repository interface — `backend/repositories/cost_repository.py`

```python
from datetime import date
from typing import List, Protocol
from backend.models.cost import DailyCostRow  # assumed existing model

class CostRepository(Protocol):
    table_name: str

    def get_daily_costs_between(self, start: date, end: date) -> List[DailyCostRow]: ...
    def get_daily_costs_for(self, day: date) -> List[DailyCostRow]: ...
```

(Implement with your actual DB/parquet reader. Keep it read-only.)

---

### 4) API route — `backend/api/anomaly.py`

```python
from fastapi import APIRouter, HTTPException, Depends
from backend.services.anomaly_service import AnomalyService
from backend.repositories.cost_repository import CostRepository
from backend.models.anomaly import TopAnomalySignal

router = APIRouter(prefix="/api/v1/cost", tags=["cost-anomalies"])

def get_cost_repository() -> CostRepository:
    # Replace with your actual factory (e.g., from config/container)
    from backend.repositories.cost_repository_impl import CostRepositoryImpl
    return CostRepositoryImpl()

@router.get("/anomalies/top", response_model=TopAnomalySignal)
def get_top_anomaly(
    repo: CostRepository = Depends(get_cost_repository),
) -> TopAnomalySignal:
    service = AnomalyService(repo)
    signal = service.get_top_anomaly_today()
    if not signal:
        raise HTTPException(status_code=404, detail="No anomaly detected today")
    return signal
```

---

### 5) Wire into app — `backend/main.py`

```python
from fastapi import FastAPI

