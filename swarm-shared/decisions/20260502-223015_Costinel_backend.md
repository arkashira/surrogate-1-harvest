# Costinel / backend

## Final Unified Implementation (≤2h)

**Endpoint:** `GET /api/v1/cost-anomaly/signal`  
**Behavior:** Read-only, no side effects, no execution. Returns the single strongest cost-anomaly signal for today (service-level) based on trailing 7-day baseline. Deterministic, fast, and safe for production.

---

## 1. Design Decisions (resolved)

- **Baseline scope:** trailing 7 days (excluding today) per `(service, account, region)` — balances stability and sensitivity.  
- **Aggregation:** daily totals per group, then compare today vs baseline.  
- **Anomaly metric:** z-score = `(today_cost − baseline_mean) / max(baseline_std, 1e-6)`.  
- **Severity thresholds:** `low=2.0`, `medium=3.0`, `high=4.0`, `critical=5.0` (|z-score|).  
- **Data fetch:** S3 only (billing exports). No external calls (HF CDN/API) during request handling.  
- **Fail-fast:** if today export missing → `204 No Content`. If insufficient baseline → `204 No Content`.  
- **Schema tolerance:** flexible column mapping for common billing exports; required fields enforced after mapping.

---

## 2. Settings

```python
# costinel/config.py
from pydantic_settings import BaseSettings
from typing import Dict

class Settings(BaseSettings):
    # Existing fields...
    billing_export_bucket: str = "s3://costinel-billing-exports"
    billing_export_prefix: str = "daily"
    baseline_days: int = 7
    anomaly_z_thresholds: Dict[str, float] = {
        "low": 2.0,
        "medium": 3.0,
        "high": 4.0,
        "critical": 5.0,
    }

    class Config:
        env_file = ".env"

settings = Settings()
```

---

## 3. Core Service (read-only)

```python
# costinel/services/cost_anomaly.py
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
import io
import boto3
from botocore.exceptions import ClientError
import pyarrow.parquet as pq
import pyarrow.csv as pcsv

from costinel.config import settings

_S3_CLIENT = boto3.client("s3")

def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    col_map: Dict[str, str] = {}
    for c in df.columns:
        low = c.strip().lower()
        if "service" in low or "product" in low:
            col_map[c] = "service"
        elif "cost" in low or "blended" in low or "unblended" in low:
            col_map[c] = "cost"
        elif "account" in low or "payer" in low or "linked" in low:
            col_map[c] = "account"
        elif "region" in low:
            col_map[c] = "region"
        elif "date" in low or "timestamp" in low or "usage" in low:
            col_map[c] = "timestamp"
    df = df.rename(columns=col_map)

    # Coerce and clean
    if "cost" in df.columns:
        df["cost"] = pd.to_numeric(df["cost"], errors="coerce").fillna(0.0)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    else:
        df["timestamp"] = pd.NaT

    required = {"service", "cost", "account", "region", "timestamp"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns after mapping: {missing}")

    df = df.dropna(subset=["service", "account", "region", "cost"])
    return df[["service", "cost", "account", "region", "timestamp"]]

def _s3_object_exists(bucket: str, key: str) -> bool:
    try:
        _S3_CLIENT.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False

def _load_daily_export(dt: datetime) -> Optional[pd.DataFrame]:
    bucket = settings.billing_export_bucket.replace("s3://", "")
    prefix = f"{settings.billing_export_prefix}/{dt:%Y-%m-%d}"

    for ext in ("parquet", "csv"):
        key = f"{prefix}.{ext}"
        if not _s3_object_exists(bucket, key):
            continue
        try:
            obj = _S3_CLIENT.get_object(Bucket=bucket, Key=key)
            body = obj["Body"].read()
            if ext == "parquet":
                df = pq.read_table(io.BytesIO(body)).to_pandas()
            else:
                df = pcsv.read_csv(io.BytesIO(body)).to_pandas()
            df = _normalize_columns(df)
            return df
        except ClientError:
            continue
    return None

def _daily_totals(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df.groupby(["service", "account", "region", pd.Grouper(key="timestamp", freq="D")], dropna=False)
        .agg(daily_cost=("cost", "sum"))
        .reset_index()
    )

def _load_baseline(dt: datetime, days: int) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    bucket = settings.billing_export_bucket.replace("s3://", "")
    prefix_base = settings.billing_export_prefix

    for i in range(1, days + 1):
        day = dt - timedelta(days=i)
        key = f"{prefix_base}/{day:%Y-%m-%d}.parquet"
        try:
            obj = _S3_CLIENT.get_object(Bucket=bucket, Key=key)
            df = pq.read_table(io.BytesIO(obj["Body"].read())).to_pandas()
        except ClientError:
            key = f"{prefix_base}/{day:%Y-%m-%d}.csv"
            try:
                obj = _S3_CLIENT.get_object(Bucket=bucket, Key=key)
                df = pcsv.read_csv(io.BytesIO(obj["Body"].read())).to_pandas()
            except ClientError:
                continue

        df = _normalize_columns(df)
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=["service", "account", "region", "timestamp", "daily_cost"])

    baseline = pd.concat(frames, ignore_index=True)
    baseline = _daily_totals(baseline)
    return baseline

def compute_top_anomaly(dt: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    if dt is None:
        dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)

    today_df = _load_daily_export(dt)
    if today_df is None or today_df.empty:
        return None

    today_totals = _daily_totals(today_df)
    baseline = _load_baseline(dt, settings.baseline_days)

    if baseline.empty or "daily_cost" not in baseline.columns:
        return None

    # Baseline stats per group
    stats = (
        baseline.groupby(["service", "account", "region"])["daily_cost"]
        .agg(baseline_mean="mean", baseline_std="std", baseline_count="count")
        .reset_index()
    )
    stats["baseline_std"] = stats["baseline_std"].fillna(0.0)

    merged = today_totals.merge(stats, on=["service", "account", "region"], how="inner")
    if merged.empty:
        return None

    merged["z_score"] = (merged["daily_cost"] - merged["baseline_mean"]) / merged["baseline_std"].replace(0, 1e-6)
    merged["abs_z"] = merged["z_score"].abs()

    # Pick top by |z-score|
    top = merged.loc[merged["abs_z"].idxmax()]

    thresholds = settings.anomaly_z_thresholds
    abs_z = float(top["abs_z"])
    if abs_z >= thresholds["critical"]:
        severity = "critical"
    elif abs_z >= thresholds["high"]:
        severity = "high"
    elif abs_z >= thresholds["
