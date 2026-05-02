# Costinel / backend

## Final Synthesized Answer  
*(Best parts merged, contradictions resolved for correctness + concrete actionability)*

---

## Chosen Improvement (≤2h, read-only)
Add a **read-only** `GET /api/v1/cost-anomaly/signal` endpoint that:
- Ingests today’s cloud billing export (CSV/Parquet) from a configured path.
- Projects to `{service, cost, account, region, timestamp}`.
- Computes per-`(service, account, region)` daily cost z-scores vs a **trailing 14-day baseline** (fallback to 7 days if insufficient data).
- Returns the **single top-cost anomaly** (highest absolute z-score meeting threshold) with deterministic, audit-friendly metadata.
- **Zero side-effects**: no writes, no execute. Aligns with “Sense + Signal — ไม่ Execute”.

---

## Resolved Contradictions
| Candidate 1 | Candidate 2 | Resolution (chosen) |
|-------------|-------------|---------------------|
| Per-service daily totals only | Per-`(service, account, region)` daily totals | Use **granular grouping** for actionable attribution; still summarize top anomaly for readability. |
| 7-day baseline | 14-day baseline | Use **14-day baseline** (Candidate 2) with fallback to 7 days if insufficient history (Candidate 1 robustness). |
| Global fallback std only | No explicit global fallback | Use **hierarchical fallback**: per-group std → per-service std → global std; clamp near-zero std to avoid divide-by-zero. |
| Hardcoded z ≥ 2.0 | Configurable thresholds | Make **threshold configurable** (env + optional query param) with default 2.0. |
| Return 204 if none | Implicit success payload | Return **200 with explicit `null`/empty fields** or 204; prefer 200 with `{ "signal": null }` for simpler client handling. |

---

## Implementation Plan (≤2h)

1. **Config** (add to existing settings/env)
   - `BILLING_EXPORT_PATH` (str)
   - `BILLING_FILE_PATTERN` (str, supports `{date}`)
   - `BASELINE_DAYS` (int, default 14)
   - `ANOMALY_Z_THRESHOLD` (float, default 2.0)
   - `CURRENCY` (str, default "USD")

2. **Service module** `services/cost_anomaly.py`
   - `load_billing(for_date, lookback_days)` → loads today + baseline files.
   - `project_billing(df)` → validates/coerces schema, drops nulls.
   - `compute_daily_costs(df)` → group by date + service + account + region, sum cost.
   - `compute_z_scores(daily, baseline_days)` → hierarchical baseline stats + robust fallback.
   - `select_top_anomaly(z_df, threshold)` → deterministic pick (max |z|, tie-break by cost).
   - `build_signal(record, context)` → deterministic payload with audit fields.

3. **FastAPI route** `routes/cost_anomaly.py`
   - `GET /api/v1/cost-anomaly/signal`
   - Query params: `date` (ISO, default today), `threshold` (float, default from env).
   - Returns 200 with `{ signal: AnomalySignal | null }`.

4. **Wire into app** (`main.py`) and mount router.

5. **Minimal tests** (`tests/`)
   - Fixture CSV/Parquet in `tests/fixtures/billing/`.
   - Tests for projection, z-score logic, and endpoint response.

Estimated time: 90–110 minutes.

---

## Code Snippets

### 1. Settings / Env

```python
# config.py (or settings.py)
import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    BILLING_EXPORT_PATH: str = os.getenv("BILLING_EXPORT_PATH", "/data/billing/export")
    BILLING_FILE_PATTERN: str = os.getenv("BILLING_FILE_PATTERN", "billing-{date}.parquet")
    BASELINE_DAYS: int = int(os.getenv("BASELINE_DAYS", "14"))
    ANOMALY_Z_THRESHOLD: float = float(os.getenv("ANOMALY_Z_THRESHOLD", "2.0"))
    CURRENCY: str = os.getenv("CURRENCY", "USD")

    class Config:
        env_file = ".env"

settings = Settings()
```

---

### 2. Service Module

```python
# services/cost_anomaly.py
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

import pandas as pd
import numpy as np
from pydantic import BaseModel, Field

from config import settings


class BillingRecord(BaseModel):
    service: str
    cost: float
    account: str
    region: str
    timestamp: datetime


class AnomalySignal(BaseModel):
    signal_id: str = Field(..., description="Deterministic signal identifier")
    date: str
    top_anomaly: Dict[str, Any]
    z_score: float
    reason: str
    context: Dict[str, Any]
    audit: Dict[str, Any]


def _file_path(for_date: date) -> Path:
    fname = settings.BILLING_FILE_PATTERN.format(date=for_date.isoformat())
    return Path(settings.BILLING_EXPORT_PATH) / fname


def _load_file(p: Path) -> pd.DataFrame:
    if not p.exists():
        alt = p.with_suffix(".csv") if p.suffix == ".parquet" else p.with_suffix(".parquet")
        if alt.exists():
            p = alt
        else:
            raise FileNotFoundError(f"Billing file not found: {p}")

    if p.suffix == ".parquet":
        return pd.read_parquet(p)
    return pd.read_csv(p)


def load_billing(for_date: Optional[date] = None, lookback_days: Optional[int] = None) -> pd.DataFrame:
    d = for_date or date.today()
    lookback = lookback_days or settings.BASELINE_DAYS

    # Load today
    today_path = _file_path(d)
    frames = [_load_file(today_path)]

    # Load baseline days (excluding today)
    for i in range(1, lookback + 1):
        prev = d - timedelta(days=i)
        try:
            frames.append(_load_file(_file_path(prev)))
        except FileNotFoundError:
            continue

    if not frames:
        raise FileNotFoundError("No billing files found for requested range.")

    df = pd.concat(frames, ignore_index=True)
    return df


def project_billing(df: pd.DataFrame) -> pd.DataFrame:
    required = {"service", "cost", "account", "region", "timestamp"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    out = df[list(required)].copy()
    out["cost"] = pd.to_numeric(out["cost"], errors="coerce")
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["cost", "timestamp"])
    return out


def compute_daily_costs(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["d"] = df["timestamp"].dt.date
    return (
        df.groupby(["d", "service", "account", "region"], as_index=False)["cost"]
        .sum()
        .sort_values("d")
    )


def compute_z_scores(daily: pd.DataFrame, baseline_days: int) -> pd.DataFrame:
    today = date.today()
    today_mask = daily["d"] == today
    today_rows = daily[today_mask].copy()
    baseline = daily[~today_mask].copy()

    if today_rows.empty or baseline.empty:
        return pd.DataFrame(columns=list(today_rows.columns) + ["z", "mean", "std"])

    # Baseline stats per group
    stats = (
        baseline.groupby(["service", "account", "region"])["cost"]
        .agg(["mean", "std", "count"])
        .reset_index()
    )

    merged = today_rows.merge(stats, on=["service", "account", "region"], how="left")

    # Hierarchical fallback
    global_mean = baseline["cost"].mean()
    global_std = baseline["cost"].std()
    if pd.isna(global_std) or global_std
