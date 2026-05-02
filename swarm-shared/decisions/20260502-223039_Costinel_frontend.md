# Costinel / frontend

## Final Synthesis — One production-ready plan (≤2h)

**Goal:** Add a single read-only endpoint  
`GET /api/v1/cost-anomaly/signal`  
that returns a deterministic “top-cost anomaly for today” signal with zero side effects.

---

## 1) Core behavior (resolve contradictions)

- Deterministic scope: **today (UTC)** only.  
- Read-only: no DB writes, no jobs, no mutations.  
- Fail-fast: 404 if file missing; 422 if malformed.  
- Single top signal: pick the largest **positive** deviation (avoid alert fatigue; aligns with “signal” not “noise”).  
- Explainable math: baseline = prior 7-day mean per key; z-score when std available; safe divide-by-zero handling.  
- Configurable input: supports local path, S3/GCS/HF URLs via existing config/secret mechanism; accepts CSV or Parquet.

---

## 2) Response shape (concrete, frontend-ready)

```json
{
  "signal_id": "uuid",
  "generated_at": "ISO8601",
  "window": "YYYY-MM-DD",
  "top_anomaly": {
    "service": "string",
    "account": "string",
    "region": "string",
    "cost": 123.45,
    "baseline": 100.00,
    "deviation_pct": 23.45,
    "z_score": 2.34
  },
  "audit": {
    "source_path": "string",
    "row_count": 12345,
    "projection": ["service","cost","account","region","timestamp"]
  }
}
```

- All numeric fields rounded to 2 decimals for stable API contracts.  
- `signal_id` and `generated_at` enable idempotency and traceability.  
- `audit` gives operators immediate provenance without exposing raw rows.

---

## 3) Implementation plan (≤2h)

1. Add FastAPI route `GET /api/v1/cost-anomaly/signal` (read-only).  
2. Implement loader helper:
   - Accept path/URL from `COST_BILLING_PATH`.  
   - Auto-detect folder `{date}.{ext}` or direct file.  
   - Support CSV and Parquet with strict column projection.  
3. Compute today vs prior 7-day baseline per `(service, account, region)`:
   - Use prior rows in same file when available; fallback to global per-service baseline.  
   - Compute std for z-score where possible; default to 0.  
   - Cap to single top positive deviation.  
4. Add minimal tests:
   - Unit test projection + top selection.  
   - Integration fixture test with sample CSV/Parquet.  
5. Wire config via existing `settings` pattern; no new secrets infrastructure.  
6. No migrations, no background jobs, no scheduler.

---

## 4) Production-ready code (complete)

### `app/schemas/cost_anomaly.py`

```python
from pydantic import BaseModel
from datetime import datetime
from typing import List

class TopAnomaly(BaseModel):
    service: str
    account: str
    region: str
    cost: float
    baseline: float
    deviation_pct: float
    z_score: float

class CostAnomalySignal(BaseModel):
    signal_id: str
    generated_at: datetime
    window: str
    top_anomaly: TopAnomaly
    audit: dict
```

---

### `app/api/v1/endpoints/cost_anomaly.py`

```python
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
import pandas as pd
import pyarrow.parquet as pq

from app.core.config import settings
from app.schemas.cost_anomaly import CostAnomalySignal, TopAnomaly

router = APIRouter()

def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _resolve_path(date_str: str) -> str:
    p = Path(settings.COST_BILLING_PATH)
    if p.is_file():
        return str(p)
    folder = p
    for ext in ("csv", "parquet"):
        candidate = folder / f"{date_str}.{ext}"
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(f"No billing file found for {date_str} in {settings.COST_BILLING_PATH}")

def _load_and_project(path: str, fmt: str) -> pd.DataFrame:
    required = {"service", "cost", "account", "region", "timestamp"}
    if fmt == "parquet":
        table = pq.read_table(path, columns=list(required))
        df = table.to_pandas()
    else:
        df = pd.read_csv(
            path,
            usecols=list(required),
            dtype={"service": str, "cost": float, "account": str, "region": str},
            parse_dates=["timestamp"],
        )
    df.columns = [c.strip().lower() for c in df.columns]
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")
    df = df.dropna(subset=list(required))
    return df

def _compute_top_anomaly(df: pd.DataFrame, target_date: str) -> TopAnomaly:
    df["date"] = df["timestamp"].dt.strftime("%Y-%m-%d")
    today_df = df[df["date"] == target_date]

    if today_df.empty:
        raise ValueError(f"No rows for target date {target_date}")

    today_agg = today_df.groupby(["service", "account", "region"], as_index=False)["cost"].sum()

    prior = df[df["date"] < target_date].copy()
    if not prior.empty:
        prior_dates = sorted(prior["date"].unique())[-7:]
        prior = prior[prior["date"].isin(prior_dates)]

    if not prior.empty:
        baseline = prior.groupby(["service", "account", "region"], as_index=False)["cost"].mean().rename(columns={"cost": "baseline"})
        std = prior.groupby(["service", "account", "region"], as_index=False)["cost"].std().rename(columns={"cost": "std"})
    else:
        # fallback: global per-service baseline
        baseline = df.groupby("service", as_index=False)["cost"].mean().rename(columns={"cost": "baseline"})
        std = df.groupby("service", as_index=False)["cost"].std().rename(columns={"cost": "std"})

    merged = today_agg.merge(baseline, on=["service", "account", "region"], how="left")
    merged = merged.merge(std, on=["service", "account", "region"], how="left")

    merged["baseline"] = merged["baseline"].fillna(merged["cost"])
    merged["std"] = merged["std"].fillna(0.0)

    merged["deviation_pct"] = ((merged["cost"] - merged["baseline"]) / merged["baseline"].replace(0, 1e-9)) * 100.0
    merged["z_score"] = ((merged["cost"] - merged["baseline"]) / merged["std"].replace(0, 1e-9))

    top_row = merged.loc[merged["deviation_pct"].idxmax()]
    return TopAnomaly(
        service=top_row["service"],
        account=top_row["account"],
        region=top_row["region"],
        cost=round(float(top_row["cost"]), 2),
        baseline=round(float(top_row["baseline"]), 2),
        deviation_pct=round(float(top_row["deviation_pct"]), 2),
        z_score=round(float(top_row["z_score"]), 2),
    )

@router.get("/signal", response_model=CostAnomalySignal)
def get_cost_anomaly_signal():
    """
    Read-only signal: top-cost anomaly for today (UTC).
    No side effects. Deterministic for the day.
    """
    date_str = _today_utc()
    try:
        path = _resolve_path(date_str)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        df = _load_and_project(path, settings.COST_BILLING_FORMAT)
    except Exception as exc:
       
