# Costinel / discovery

## Final Synthesis — One Correct, Actionable Plan

**Core diagnosis (merged & resolved)**  
- There is **no discovery surface** for multi-cloud cost anomalies and no ingestion for billing exports (AWS CUR, GCP BigQuery export, Azure EA/invoice).  
- There is **no signal pipeline** to convert raw telemetry into ranked, actionable recommendations.  
- There is **no lightweight CLI or scheduler** to run low-friction discovery jobs and verify data freshness/coverage.

**Chosen approach**  
Implement a **read-only discovery module** plus a **scheduler-ready job** that normalizes billing exports, runs lightweight anomaly detection, and emits ranked signals as JSON + human-readable output. Keep scope minimal (<200 lines), no UI changes, no external state mutation.

---

## Implementation (single coherent plan)

### File layout
```
src/discovery/
├── __init__.py
├── cli.py
├── anomaly.py
├── normalize.py
└── job.py          # scheduler-friendly entrypoint (optional but recommended)
```

### normalize.py  (corrected + robust)
```python
from __future__ import annotations
import pandas as pd
from pathlib import Path
from typing import Literal

Source = Literal["aws", "gcp", "azure"]

COLUMNS: dict[Source, dict[str, str]] = {
    "aws": {
        "cost": "BlendedCost",
        "usage": "UsageQuantity",
        "account": "LinkedAccountId",
        "service": "ProductCode",
        "timestamp": "UsageStartDate",
    },
    "gcp": {
        "cost": "cost",
        "usage": "usage_amount",
        "account": "project_id",
        "service": "service_description",
        "timestamp": "usage_start_time",
    },
    "azure": {
        "cost": "Cost",
        "usage": "Quantity",
        "account": "SubscriptionId",
        "service": "ServiceName",
        "timestamp": "UsageDate",
    },
}

def _infer_source(path: Path, source: str | None) -> Source:
    if source is not None:
        if source not in COLUMNS:
            raise ValueError(f"source must be one of {list(COLUMNS)}")
        return source  # type: ignore[return-value]

    name = path.name.lower()
    if "cur" in name or "aws" in name:
        return "aws"
    if "gcp" in name or "bigquery" in name:
        return "gcp"
    if "azure" in name or "ea" in name:
        return "azure"
    raise ValueError("Cannot infer source; provide --source explicitly.")

def normalize(path: Path, source: str | None = None) -> pd.DataFrame:
    src = _infer_source(path, source)
    mapping = COLUMNS[src]

    if path.suffix.lower() == ".parquet":
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path)

    # Normalize column names
    df.columns = [c.strip().replace(".", "_").lower() for c in df.columns]
    # Remap keys similarly
    lookup = {k.lower().replace(".", "_"): v.lower().replace(".", "_") for k, v in mapping.items()}

    # Resolve actual column names in df
    col_map: dict[str, str] = {}
    for canonical, target in lookup.items():
        matches = [c for c in df.columns if c.lower().replace(".", "_") == canonical]
        if not matches:
            raise KeyError(f"Missing required column for {canonical} in {src} file")
        col_map[matches[0]] = target

    df = df.rename(columns=col_map)

    # Canonical schema
    out = pd.DataFrame({
        "timestamp": pd.to_datetime(df["timestamp"], errors="coerce"),
        "account": df["account"].astype(str),
        "service": df["service"].astype(str),
        "cost": pd.to_numeric(df["cost"], errors="coerce").fillna(0.0),
        "usage": pd.to_numeric(df["usage"], errors="coerce").fillna(0.0),
        "source": src,
    })
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return out
```

### anomaly.py  (IQR + rate-of-change, severity-ranked)
```python
from __future__ import annotations
import pandas as pd
import numpy as np
from typing import Any, Dict, List

def _iqr_outliers(series: pd.Series, k: float = 1.5) -> pd.Series:
    q1, q3 = series.quantile(0.25), series.quantile(0.75)
    iqr = q3 - q1
    lower = q1 - k * iqr
    upper = q3 + k * iqr
    return (series < lower) | (series > upper)

def discover_signals(df: pd.DataFrame, top_n: int = 10) -> List[Dict[str, Any]]:
    # Daily rollups per account/service
    daily = (
        df.groupby([pd.Grouper(key="timestamp", freq="D"), "account", "service"])
        .agg(cost=("cost", "sum"), usage=("usage", "sum"))
        .reset_index()
    )

    # IQR outliers per account/service
    daily["cost_iqr_outlier"] = daily.groupby(["account", "service"])["cost"].transform(
        lambda x: _iqr_outliers(x)
    )

    # Day-over-day rate of change
    daily = daily.sort_values(["account", "service", "timestamp"])
    daily["cost_prev"] = daily.groupby(["account", "service"])["cost"].shift(1)
    daily["roc"] = (daily["cost"] - daily["cost_prev"]) / (daily["cost_prev"] + 1e-12)
    daily["roc_spike"] = daily["roc"] > 1.0  # >100% increase

    signals: List[Dict[str, Any]] = []
    for _, row in daily.iterrows():
        reasons: List[str] = []
        severity = "low"

        if row["cost_iqr_outlier"]:
            reasons.append("cost_outlier_iqr")
            severity = "medium"
        if row["roc_spike"]:
            reasons.append("cost_spike_roc_gt_100pct")
            severity = "high"

        if reasons:
            signals.append({
                "timestamp": row["timestamp"].isoformat(),
                "account": row["account"],
                "service": row["service"],
                "cost": round(float(row["cost"]), 4),
                "usage": round(float(row["usage"]), 4),
                "reasons": reasons,
                "severity": severity,
                "context": {
                    "daily_cost": round(float(row["cost"]), 2),
                    "daily_usage": round(float(row["usage"]), 2),
                    "day_over_day_roc_pct": round(float(row["roc"]) * 100.0, 1),
                },
            })

    severity_order = {"high": 0, "medium": 1, "low": 2}
    signals.sort(key=lambda s: (severity_order.get(s["severity"], 3), -s["cost"]))
    return signals[:top_n]
```

### cli.py  (lightweight, human + machine output)
```python
#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

# Ensure imports resolve when run as module
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from rich.console import Console
from rich.table import Table

from discovery.normalize import normalize
from discovery.anomaly import discover_signals

console = Console()

def _build_table(signals):
    table = Table(title="Costinel — Discovery Signals (Sense + Signal)")
    table.add_column("Severity", style="cyan")
    table.add_column("Account", style="magenta")
    table.add_column("Service", style="green")
    table.add_column("Cost", justify="right", style="yellow")
    table.add_column("Reasons", style="white")
    table.add_column("Timestamp", style="dim")
    for s in signals:
        table.add_row(
            s["severity"].upper(),
            s["account"],
            s["service"],
            f"${s['
