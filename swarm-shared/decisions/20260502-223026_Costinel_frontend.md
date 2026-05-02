# Costinel / frontend

## Final Unified Implementation (Best Parts Merged)

**Chosen improvement:**  
Add a **read-only** `GET /api/v1/cost-anomaly/signal` endpoint that deterministically returns the single highest-cost anomaly for **today** using service-level z-scores against a 30-day baseline. Zero side effects, no state changes, no execute path.

---

## Why This Wins
- **Fastest to ship** (≤2h): pure read path, no migrations, no auth changes, no background jobs.  
- **Leverages existing billing exports** (CSV/Parquet) already landing in buckets.  
- **Deterministic & reproducible**: same input → same top anomaly; aligns with “Sense + Signal — ไม่ Execute.”  
- **Actionable output**: one clear card on the dashboard showing service, account, region, cost, deviation, and timestamp.

---

## Implementation Plan (≤2h)

### Assumptions
- Frontend: React + TypeScript (standard for AXENTX).  
- Backend: FastAPI (Python).  
- Billing exports: `s3://costinel-billing/YYYY/MM/DD/billing.parquet` (CSV fallback).  
- Columns available: `service`, `cost`, `account`, `region`, `timestamp`.

### Steps (timeboxed)

1. **Backend: add FastAPI route** (15–25 min)  
   - Read today’s file from configured bucket path (env var).  
   - Project and coerce types.  
   - Build 30-day baseline from prior days; compute per-service `mean` and `std`.  
   - Calculate z-score for today’s service rows; return top positive z-score.  
   - Read-only, deterministic, no mutations.

2. **Frontend: signal display component** (20–30 min)  
   - Create `CostAnomalySignal` component.  
   - Use SWR (or React Query) to fetch `/api/v1/cost-anomaly/signal`.  
   - Show card with service, account, region, cost, baseline mean, z-score, timestamp.  
   - Reuse design tokens; minimal, clean UI with an alert icon.

3. **Wire into dashboard** (10–15 min)  
   - Place component in main cost dashboard.  
   - Render only when data exists; handle loading/error states gracefully.

4. **Tests & validation** (10–15 min)  
   - Smoke test with sample billing file.  
   - Verify endpoint shape and HTTP 200/404/500 behavior.  
   - Verify frontend renders without errors and refreshes safely.

5. **Cleanup & docs** (5–10 min)  
   - Inline comments; update API README if present.  
   - Note env var requirement and expected file layout.

---

## Code Snippets

### Backend: FastAPI route

`backend/routes/cost_anomaly.py`
```python
import os
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])

BILLING_BUCKET_PATH = os.getenv("BILLING_BUCKET_PATH", "s3://costinel-billing")

class AnomalySignal(BaseModel):
    service: str
    account: str
    region: str
    cost: float
    timestamp: datetime
    z_score: float
    baseline_mean: float
    baseline_std: float

def load_billing_for_date(target_date: datetime) -> pd.DataFrame:
    date_path = (
        f"{target_date:%Y}/{target_date:%m}/{target_date:%d}"
    )
    parquet_path = os.path.join(BILLING_BUCKET_PATH, date_path, "billing.parquet")
    csv_path = parquet_path.replace(".parquet", ".csv")

    if os.path.exists(parquet_path):
        df = pd.read_parquet(parquet_path)
    elif os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
    else:
        raise FileNotFoundError(f"Billing file not found for {target_date:%Y-%m-%d}")

    required = {"service", "cost", "account", "region", "timestamp"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns in billing file: {missing}")

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["cost"] = pd.to_numeric(df["cost"], errors="coerce")
    return df[["service", "cost", "account", "region", "timestamp"]].dropna()

def compute_top_anomaly(today_df: pd.DataFrame, baseline_days: int = 30) -> Optional[AnomalySignal]:
    today_date = today_df["timestamp"].max().normalize()

    baseline_dfs = []
    for i in range(1, baseline_days + 1):
        prev_date = today_date - timedelta(days=i)
        try:
            df_prev = load_billing_for_date(prev_date)
            baseline_dfs.append(df_prev)
        except Exception:
            continue

    if not baseline_dfs:
        raise HTTPException(status_code=404, detail="No baseline data available for anomaly detection")

    baseline = pd.concat(baseline_dfs, ignore_index=True)

    # Daily service-level spend for baseline
    baseline_daily = (
        baseline.groupby(["service", baseline["timestamp"].dt.date])["cost"]
        .sum()
        .reset_index()
    )
    stats = (
        baseline_daily.groupby("service")["cost"]
        .agg(["mean", "std"])
        .reset_index()
        .rename(columns={"mean": "baseline_mean", "std": "baseline_std"})
    )

    # Today's service-level spend
    today_daily = (
        today_df.groupby("service")["cost"]
        .sum()
        .reset_index()
        .rename(columns={"cost": "today_cost"})
    )

    merged = today_daily.merge(stats, on="service", how="left")
    merged["baseline_std"] = merged["baseline_std"].fillna(merged["baseline_mean"] * 0.1)
    merged["z_score"] = (merged["today_cost"] - merged["baseline_mean"]) / merged["baseline_std"]
    merged = merged.dropna(subset=["z_score"])

    if merged.empty:
        return None

    top = merged.loc[merged["z_score"].idxmax()]

    # Pick a representative row for account/region/timestamp (most costly today for that service)
    sample = today_df[today_df["service"] == top["service"]].sort_values("cost", ascending=False).iloc[0]

    return AnomalySignal(
        service=top["service"],
        account=sample["account"],
        region=sample["region"],
        cost=float(top["today_cost"]),
        timestamp=today_df["timestamp"].max(),
        z_score=float(top["z_score"]),
        baseline_mean=float(top["baseline_mean"]),
        baseline_std=float(top["baseline_std"]),
    )

@router.get("/signal", response_model=AnomalySignal)
def get_cost_anomaly_signal():
    today = datetime.utcnow().date()
    try:
        today_df = load_billing_for_date(datetime.combine(today, datetime.min.time()))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load billing data: {str(e)}")

    signal = compute_top_anomaly(today_df)
    if signal is None:
        raise HTTPException(status_code=404, detail="No anomaly detected today")
    return signal
```

Register route in main app:
```python
# backend/main.py (or app.py)
from fastapi import FastAPI
from backend.routes.cost_anomaly import router as anomaly_router

app = FastAPI()
app.include_router(anomaly_router)
```

---

### Frontend: React component

`src/components/CostAnomalySignal.tsx`
```tsx
import useSWR from 'swr';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { AlertTriangle } from 'lucide-react';

interface AnomalySignal {
  service: string;
  account: string;
  region: string;
  cost: number;
  timestamp: string;
  z_score
