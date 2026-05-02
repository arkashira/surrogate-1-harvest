# Costinel / backend

## Final synthesis (best of both proposals)

**Chosen improvement (≤2h):**  
Add a backend “cost-anomaly signal” endpoint that ingests daily cost exports from cloud billing buckets (CSV/Parquet), computes deterministic anomaly scores (z-score on a 30-day rolling window), and exposes read-only, audit-ready signals via API.  
- Completes the **Sense** half of `Sense + Signal — لا Execute` for Costinel.  
- Reuses existing stores (Postgres + ingestion pipeline) and adds lightweight file ingestion.  
- Frontend can render signals immediately.  
- Fits in <2h: one model, one ingestion task, one service, one route, minimal tests.

---

## Implementation plan (concrete + actionable)

1. **Model** – `CostAnomalySignal` (immutable, append-only)  
   - Same schema as Candidate 1, plus `source_file` and `ingested_at` for provenance.  
   - Indexes: `(date, cloud, severity)`, `(signal_type, severity)`, `(account_id, date)`.

2. **Ingestion** (lightweight, idempotent)  
   - Accept daily CSV/Parquet from configured billing buckets (or local path for dev).  
   - Normalize to `DailySpend(date, cloud, account_id, service, spend_usd, currency, raw_line_items, source_file, ingested_at)`.  
   - Idempotency key: `(date, cloud, account_id, service, source_file)` to avoid double-counting.  
   - Keep ingestion separate from detection to preserve determinism.

3. **Detection logic** (`CostAnomalyService`)  
   - Input: yesterday’s per-account-service daily spend (from `DailySpend`).  
   - Rolling window: last 30 days (excluding current).  
   - Compute: mean, std (ddof=1), z = (value − mean) / std.  
   - Thresholds: |z| ≥ 2.5 → medium, ≥ 3.5 → high.  
   - Deterministic: pure function; store snapshot of window in `meta.snapshot`.  
   - Edge cases:  
     - If std = 0 → skip (no variance).  
     - If window < 7 days → skip (insufficient history).  
     - Negative spend → treat as invalid; log and skip.

4. **API endpoint**  
   - `GET /api/v1/signals/cost-anomalies`  
   - Query params: `from`, `to`, `cloud`, `account_id`, `severity`, `limit`, `offset`.  
   - Response: paginated list + summary counts.  
   - Strict validation on dates and limit.

5. **CLI task**  
   - `python -m axentx.costinel.tasks.run_anomaly_detection --date 2026-05-03`  
   - Idempotent upsert on `(date, cloud, account_id, service, metric, source_file)` (or deterministic composite).  
   - Optional dry-run flag to preview signals without write.

6. **Tests**  
   - Unit: detection math (zero std, small window, negative values).  
   - Integration: ingestion idempotency, endpoint filters, pagination.  
   - Snapshot test: given fixed history, output signals are deterministic.

7. **Migrations + seeds**  
   - Create tables + indexes.  
   - Backfill last 7 days (safe, append-only).  
   - Add sample ingestion file for dev/test.

---

## Code snippets (merged + hardened)

### Model (`models/cost_anomaly_signal.py`)
```python
from sqlmodel import SQLModel, Field, JSON
from datetime import date, datetime
from typing import Optional, Dict, Any
from uuid import UUID, uuid4
from enum import Enum

class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class CostAnomalySignal(SQLModel, table=True):
    __tablename__ = "cost_anomaly_signals"

    id: UUID = Field(default_factory=uuid4, primary_key=True, index=True)
    date: date = Field(index=True, nullable=False)
    cloud: str = Field(index=True, nullable=False)          # aws | gcp | azure
    account_id: str = Field(index=True, nullable=False)
    service: str = Field(index=True, nullable=False)
    metric: str = Field(default="daily_spend_usd", index=True)
    value: float = Field(nullable=False)
    baseline_mean: float = Field(nullable=False)
    baseline_std: float = Field(nullable=False)
    z_score: float = Field(nullable=False)
    severity: Severity = Field(index=True, nullable=False)
    signal_type: str = Field(default="cost_spike", index=True)
    source_file: Optional[str] = Field(default=None, index=True)
    tags: Dict[str, Any] = Field(sa_type=JSON, default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    meta: Dict[str, Any] = Field(sa_type=JSON, default_factory=dict)

    class Config:
        arbitrary_types_allowed = True
```

### DailySpend model (ingestion target)
```python
from sqlmodel import SQLModel, Field, JSON
from datetime import date
from typing import Optional, Dict, Any
from uuid import UUID, uuid4

class DailySpend(SQLModel, table=True):
    __tablename__ = "daily_spend"

    id: UUID = Field(default_factory=uuid4, primary_key=True, index=True)
    date: date = Field(index=True, nullable=False)
    cloud: str = Field(index=True, nullable=False)
    account_id: str = Field(index=True, nullable=False)
    service: str = Field(index=True, nullable=False)
    spend_usd: float = Field(nullable=False)
    currency: str = Field(default="USD")
    raw_line_items: Optional[Dict[str, Any]] = Field(sa_type=JSON, default=None)
    source_file: Optional[str] = Field(default=None, index=True)
    ingested_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

    class Config:
        arbitrary_types_allowed = True
```

### Ingestion helper (`services/ingest_cost_exports.py`)
```python
from typing import List, Optional
from datetime import date, datetime
from pathlib import Path
import pandas as pd
from db import SessionDep
from models.daily_spend import DailySpend
import logging

logger = logging.getLogger(__name__)

class CostExportIngester:
    @staticmethod
    def ingest_file(session: SessionDep, path: str, cloud: str) -> int:
        df = pd.read_csv(path) if str(path).endswith(".csv") else pd.read_parquet(path)
        # Expected columns: date, account_id, service, spend_usd, [currency], [line_items]
        rows: List[DailySpend] = []
        seen = set()

        for _, r in df.iterrows():
            d = r["date"] if isinstance(r["date"], date) else pd.to_datetime(r["date"]).date()
            key = (d, cloud, str(r["account_id"]), str(r["service"]), str(path))
            if key in seen:
                continue
            seen.add(key)

            spend = float(r["spend_usd"])
            if spend < 0:
                logger.warning("Negative spend skipped: %s %s", d, r["account_id"])
                continue

            rows.append(
                DailySpend(
                    date=d,
                    cloud=cloud,
                    account_id=str(r["account_id"]),
                    service=str(r["service"]),
                    spend_usd=spend,
                    currency=str(r.get("currency", "USD")),
                    raw_line_items=r.get("line_items"),
                    source_file=str(path),
                )
            )

        # Idempotent insert: skip existing (date,cloud,account_id,service,source_file)
        for row in rows:
            exists = session.exec(
                DailySpend.select()
                .where(DailySpend.date == row.date)
                .where(DailySpend.cloud == row.cloud)
                .where(DailySpend.account_id == row.account_id)
                .where(DailySpend.service == row.service)
                .where(DailySpend.source_file == row.source_file)
            ).first()
            if not exists:
                session.add(row)

        session.commit()
        return len(rows)
```

### Detection service (`services/cost_anomaly_service.py`)
```python
from typing import List
from datetime import date
from db
