# Costinel / frontend

## Final Synthesis (Best Parts + Correctness + Actionability)

I merged the strongest elements from both proposals and resolved contradictions in favor of correctness and immediate actionability.

**Key decisions:**
- Use **Candidate 1’s IQR-based, per-service daily aggregation** for anomaly detection (robust, deterministic, explainable) — but fix its bug where it returns *daily total* instead of *top anomalous line item(s)*.
- Adopt **Candidate 2’s response shape** (`signal_id`, `detected_at`, `severity`, `summary`) because it is frontend-ready and aligns with alerting UX.
- Keep **read-only, zero-side-effect** behavior and configurable billing path/format.
- Add **graceful fallback behavior** (204 when no data/no anomaly) and lightweight validation.

---

## Implementation Plan (≤2h)

1. Add minimal dependencies if missing: `pandas`, `fastapi` (already typical).
2. Create `app/api/v1/endpoints/cost_anomaly.py` with:
   - Configurable `COSTINEL_BILLING_PATH` and `COSTINEL_BILLING_FORMAT`
   - Robust column mapping and type coercion
   - Deterministic anomaly detection (IQR per service on daily totals)
   - Return the **top-cost anomalous line item(s)** for today with explainable fields
3. Expose `GET /api/v1/cost-anomaly/signal` returning:
   - `signal_id`, `detected_at`, `severity`, `summary`, `top_anomaly`
   - 204 when no data or no anomaly
4. Wire router into main app.
5. (Optional) Add a minimal frontend card that polls this endpoint.

---

## Final Code

### `app/api/v1/endpoints/cost_anomaly.py`

```python
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException

router = APIRouter()

BILLING_PATH = os.getenv("COSTINEL_BILLING_PATH", "/data/billing/today.csv")
BILLING_FORMAT = os.getenv("COSTINEL_BILLING_FORMAT", "csv").lower()

# Flexible column mapping
COLS = {
    "service": ["service", "productcode", "product_name", "product"],
    "cost": ["cost", "unblendedcost", "amount", "blendedcost"],
    "account": ["account", "accountid", "linked_account_id", "payer_account_id"],
    "region": ["region", "regionname", "availabilityzone", "az"],
    "timestamp": ["timestamp", "usagestartdate", "usage_start_time", "date"],
}

def _find_col(df: pd.DataFrame, candidates: list) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None

def _load_billing() -> pd.DataFrame:
    if not os.path.exists(BILLING_PATH):
        raise HTTPException(status_code=404, detail=f"Billing path not found: {BILLING_PATH}")

    if BILLING_FORMAT == "parquet":
        df = pd.read_parquet(BILLING_PATH)
    else:
        df = pd.read_csv(BILLING_PATH)

    if df.empty:
        raise HTTPException(status_code=204, detail="No billing data available")
    return df

def _project_and_clean(df: pd.DataFrame) -> pd.DataFrame:
    sc = _find_col(df, COLS["service"])
    cc = _find_col(df, COLS["cost"])
    ac = _find_col(df, COLS["account"])
    rc = _find_col(df, COLS["region"])
    tc = _find_col(df, COLS["timestamp"])

    if not sc or not cc:
        raise HTTPException(
            status_code=500,
            detail=f"Required columns missing. Found: {list(df.columns)}"
        )

    out = df.copy()
    out["_service"] = out[sc].astype(str)
    out["_cost"] = pd.to_numeric(out[cc], errors="coerce").fillna(0.0)

    out["_account"] = str(ac) if not ac else out[ac].astype(str)
    out["_region"] = "unknown" if not rc else out[rc].astype(str)
    if tc:
        out["_timestamp"] = pd.to_datetime(out[tc], errors="coerce", utc=True)
    else:
        out["_timestamp"] = pd.Timestamp.now(tz=timezone.utc)

    out = out.dropna(subset=["_cost"])
    return out[["_service", "_cost", "_account", "_region", "_timestamp"]].rename(
        columns={
            "_service": "service",
            "_cost": "cost",
            "_account": "account",
            "_region": "region",
            "_timestamp": "timestamp",
        }
    )

def _detect_top_anomaly(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    # Daily totals per service for IQR rule
    daily = df.groupby("service")["cost"].sum().reset_index()
    costs = daily["cost"].values

    if len(costs) < 2:
        return None

    q1 = float(pd.Series(costs).quantile(0.25))
    q3 = float(pd.Series(costs).quantile(0.75))
    iqr = q3 - q1
    upper_bound = q3 + 1.5 * iqr

    # Identify anomalous services
    anomalous_services = daily[daily["cost"] > upper_bound]["service"].tolist()
    if not anomalous_services:
        return None

    # Pick top anomalous line item(s) today among those services
    candidates = df[df["service"].isin(anomalous_services)]
    top_row = candidates.loc[candidates["cost"].idxmax()]

    service_total = float(daily.loc[daily["service"] == top_row["service"], "cost"].iloc[0])
    severity = "high" if service_total > upper_bound * 1.5 else "medium"

    return {
        "signal_id": str(uuid.uuid4()),
        "detected_at": datetime.now(tz=timezone.utc).isoformat(),
        "severity": severity,
        "summary": (
            f"Service '{top_row['service']}' shows anomalous spend. "
            f"Top line item: {top_row['cost']:.2f} (service total today: {service_total:.2f})."
        ),
        "top_anomaly": {
            "service": top_row["service"],
            "cost": float(top_row["cost"]),
            "account": str(top_row["account"]),
            "region": str(top_row["region"]),
            "timestamp": top_row["timestamp"].isoformat(),
            "service_total_today": round(service_total, 4),
            "upper_bound": round(upper_bound, 4),
            "anomaly_score": round((float(top_row["cost"]) - upper_bound) / (upper_bound + 1e-9), 4),
            "signal_reason": f"Service daily total {round(service_total, 2)} exceeds upper bound {round(upper_bound, 2)} (IQR rule)",
        },
    }

@router.get("/signal")
def get_cost_anomaly_signal() -> Optional[Dict[str, Any]]:
    """
    Read-only signal endpoint.
    Returns a compact anomaly signal for today or 204 if none.
    """
    try:
        raw = _load_billing()
    except HTTPException as e:
        if e.status_code == 204:
            return None
        raise

    projected = _project_and_clean(raw)
    if projected.empty:
        raise HTTPException(status_code=204, detail="No valid billing rows")

    signal = _detect_top_anomaly(projected)
    if not signal:
        raise HTTPException(status_code=204, detail="No anomaly detected")
    return signal
```

### Wire into main app (`app/main.py`)

```python
from fastapi import FastAPI
from app.api.v1.endpoints import cost_anomaly

app = FastAPI(title="Costinel API")
app.include_router(cost_anomaly.router, prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])
```

---

## Quick Validation

```bash
# Simulate billing file
mkdir -p /data/billing
cat > /data/billing/today.csv <<'EOF'
productcode
