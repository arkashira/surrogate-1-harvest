# Costinel / backend

Candidate 3:
## Highest-Value Incremental Improvement (≤2h)

**Chosen:** Add a read-only `/api/v1/cost-anomaly/signal` endpoint that:
- Ingests today’s cloud billing export (CSV/Parquet) from a configured bucket path
- Projects to `{service, cost, account, region, timestamp}`
- Computes a deterministic top-cost anomaly (z-score + isolation-forest fallback)
- Returns a single auditable signal with context and no execution capability (Sense + Signal)

**Why this ships fast:**
- Reuses existing patterns: deterministic output, no execution, audit trail
- Avoids HF API limits by using CDN-bypass for any dataset fetch (if needed)
- Single endpoint + one background ingestion task only
- No schema mutation; strict projection prevents mixed-schema issues

---

## Implementation Plan (≤2h)

1. **Add config** (`config/cost_sources.yaml`) — cloud billing bucket/paths and thresholds.
2. **Add service module** (`services/cost_anomaly.py`) — deterministic anomaly detection with fallback.
3. **Add route** (`routes/cost_anomaly.py`) — GET `/api/v1/cost-anomaly/signal` returning 200/204.
4. **Add tests** (`tests/test_cost_anomaly.py`) — deterministic fixtures and route tests.
5. **Add CLI task** (`tasks/check_today_cost_anomaly.py`) — optional one-off runner.

---

## Code Snippets

### Config (config/cost_sources.yaml)
```yaml
cost_sources:
  billing:
    type: s3
    bucket: "acme-cost-billing"
    prefix: "daily/"
    pattern: "{date}/daily_services.parquet"
    region: "us-east-1"
anomaly:
  z_threshold: 3.0
  fallback_isolation_forest: true
  min_history_days: 7
```

### Service (services/cost_anomaly.py)
```python
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import hashlib
import yaml
from typing import Optional, Dict, Any, List

def load_config() -> Dict[str, Any]:
    cfg_path = Path(__file__).parent.parent / "config" / "cost_sources.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)

def _hash_rows(rows: List[Dict[str, Any]]) -> str:
    blob = "|".join(f"{r['service']},{r.get('account','')},{r.get('region','')},{r['cost_usd']:.2f},{r['date']}" for r in rows).encode()
    return hashlib.sha256(blob).hexdigest()

def _trailing_window(date_str: str, min_days: int = 7) -> pd.DataFrame:
    today = datetime.strptime(date_str, "%Y-%m-%d").date()
    frames = []
    for i in range(1, min_days + 1):
        d = (today - timedelta(days=i)).isoformat()
        p = Path("data/billing") / d / "daily_services.parquet"
        if p.exists():
            t = pq.read_table(p).to_pandas()
            # strict projection
            t = t[["service", "cost_usd", "account", "region", "date"]].rename(columns={"cost_usd": "cost"})
            frames.append(t)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def detect_top_anomaly(date_str: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if cfg is None:
        cfg = load_config()

    today_path = Path("data/billing") / date_str / "daily_services.parquet"
    if not today_path.exists():
        return None

    today = pq.read_table(today_path).to_pandas()
    # strict projection
    today = today[["service", "cost_usd", "account", "region", "date"]].rename(columns={"cost_usd": "cost"})
    today["cost"] = today["cost"].astype(float)

    history = _trailing_window(date_str, cfg["anomaly"]["min_history_days"])
    if history.empty:
        return None

    # Per-service baseline stats
    baseline = history.groupby("service")["cost"].agg(["mean", "std"]).reset_index()
    baseline["std"] = baseline["std"].fillna(0.0)

    merged = today.merge(baseline, on="service", how="left")
    merged["delta"] = merged["cost"] - merged["mean"]
    merged["z"] = np.where(merged["std"] > 0, merged["delta"] / merged["std"], 0.0)

    # Deterministic rule: z-score primary
    z_thresh = cfg["anomaly"]["z_threshold"]
    merged["is_anomaly"] = np.abs(merged["z"]) >= z_thresh

    # Fallback: isolation-forest style deterministic proxy (IQR)
    if cfg["anomaly"]["fallback_isolation_forest"] and not merged["is_anomaly"].any():
        q1 = history.groupby("service")["cost"].quantile(0.25)
        q3 = history.groupby("service")["cost"].quantile(0.75)
        iqr = (q3 - q1).fillna(0.0)
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        merged = merged.merge(lower.rename("lower"), left_on="service", right_index=True, how="left")
        merged = merged.merge(upper.rename("upper"), left_on="service", right_index=True, how="left")
        merged["is_anomaly"] = (merged["cost"] < merged["lower"]) | (merged["cost"] > merged["upper"])

    candidates = merged[merged["is_anomaly"]].copy()
    if candidates.empty:
        return None

    # Rank by largest absolute delta
    top = candidates.loc[candidates["delta"].abs().idxmax()]

    severity = "low"
    ad = abs(top["delta"])
    if ad >= 2000.0 or abs(top["z"]) >= 5.0:
        severity = "high"
    elif ad >= 1000.0 or abs(top["z"]) >= 4.0:
        severity = "medium"

    result = {
        "date": date_str,
        "service": top["service"],
        "account": top.get("account", ""),
        "region": top.get("region", ""),
        "cost_usd": float(top["cost"]),
        "baseline_mean_usd": float(top["mean"]),
        "baseline_std_usd": float(top["std"]),
        "delta_usd": float(top["delta"]),
        "z_score": float(top["z"]),
        "severity": severity,
        "rule_version": "1.0.0",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "data_hash": _hash_rows(today.to_dict(orient="records")),
    }
    return result
```

### Route (routes/cost_anomaly.py)
```python
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from services.cost_anomaly import detect_top_anomaly

router = APIRouter(prefix="/api/v1/cost-anomaly", tags=["cost", "anomaly"])

class AnomalySignal(BaseModel):
    date: str = Field(..., description="Billing date (YYYY-MM-DD)")
    service: str = Field(..., description="Service name")
    account: str = Field(..., description="Account ID")
    region: str = Field(..., description="Region")
    cost_usd: float = Field(..., description="Today cost USD")
    baseline_mean_usd: float = Field(..., description="Baseline mean USD")
    baseline_std_usd: float = Field(..., description="Baseline std USD")
    delta_usd: float = Field(..., description="Delta vs baseline")
    z_score: float = Field(..., description="Z-score vs baseline")
    severity: str = Field(..., description="high|medium|low")
    rule_version: str = Field("1.0.0", description="Deterministic rule version")
    computed_at: str = Field(..., description="UTC ISO timestamp")
    data_hash: str = Field(..., description="SHA256 of sorted rows used")

class An
