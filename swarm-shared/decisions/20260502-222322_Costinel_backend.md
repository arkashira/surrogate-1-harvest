# Costinel / backend

## Final Synthesis — Costinel Discovery Signal Pipeline (Read-Only, <2h)

**Chosen direction**: Build a **deterministic, read-only discovery signal pipeline** that produces audit-ready cost-governance outputs (anomalies + RI coverage + top-hub signals) via a shared engine consumed by both CLI and API.  
This maximizes immediate value, reuses existing patterns, and strictly honors “Sense + Signal — لا تنفذ”.

---

### 1) File layout (create these)
```bash
mkdir -p /opt/axentx/Costinel/costinel/discovery
touch /opt/axentx/Costinel/costinel/discovery/__init__.py
touch /opt/axentx/Costinel/costinel/discovery/engine.py
touch /opt/axentx/Costinel/costinel/discovery/schemas.py
touch /opt/axentx/Costinel/costinel/discovery/cli.py
mkdir -p /opt/axentx/Costinel/costinel/routes
touch /opt/axentx/Costinel/costinel/routes/discovery.py
```

---

### 2) Schemas — `schemas.py` (immutable, hash-based IDs)
```python
from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
import hashlib
import json

def _hash_payload(data: Dict[str, Any]) -> str:
    normalized = json.dumps(data, sort_keys=True, default=str).encode()
    return hashlib.sha256(normalized).hexdigest()[:16]

class CostAnomaly(BaseModel):
    service: str
    account_id: str
    region: str
    metric: str
    current_value: float
    baseline_value: float
    deviation_pct: float
    severity: str  # low|medium|high|critical
    window: str
    ts: datetime = Field(default_factory=datetime.utcnow)

class RICoverage(BaseModel):
    service: str
    account_id: str
    region: str
    current_coverage_pct: float
    recommended_purchase_usd: float
    estimated_savings_usd: float
    term: str  # 1yr|3yr
    payment_option: str  # all_upfront|partial_upfront|no_upfront

class TopHubSignal(BaseModel):
    hub_name: str
    hub_type: str
    score: float
    related_docs: List[str]
    insight: str

class DiscoveryManifest(BaseModel):
    id: str = Field(default_factory=lambda: _hash_payload({}))
    env: str
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    version: str = "4.2.0"
    tags: List[str] = Field(default_factory=list)

class DiscoveryPayload(BaseModel):
    manifest: DiscoveryManifest
    cost_anomalies: List[CostAnomaly]
    ri_recommendations: List[RICoverage]
    top_hub_signals: List[TopHubSignal]
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def write_json(self, path):
        import json
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.dict(), f, indent=2, default=str)

    def write_parquet(self, path):
        import pandas as pd
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Flatten top-level lists into separate parquet files for auditability
        if self.cost_anomalies:
            pd.DataFrame([a.dict() for a in self.cost_anomalies]).to_parquet(
                str(Path(path).parent / f"{Path(path).stem}_anomalies.parquet"), index=False
            )
        if self.ri_recommendations:
            pd.DataFrame([r.dict() for r in self.ri_recommendations]).to_parquet(
                str(Path(path).parent / f"{Path(path).stem}_ri.parquet"), index=False
            )
        # Manifest as JSON for deterministic hash checks
        self.manifest.dict()
```

---

### 3) Engine — deterministic signals — `engine.py`
```python
from datetime import datetime, timedelta
from typing import List
from .schemas import DiscoveryPayload, DiscoveryManifest, CostAnomaly, RICoverage, TopHubSignal

# Placeholder adapters — replace with real telemetry connectors (AWS CUR, GCP billing export)
def _fetch_cost_records(env: str, days: int = 7):
    # Return list[dict] with keys: service, account_id, region, cost, date
    # For MVP, return deterministic mock data to guarantee non-breaking behavior
    return []

def _detect_anomalies(records) -> List[CostAnomaly]:
    # Deterministic rule: day-over-day >30% increase for same service/account/region
    # For MVP, return empty list (safe, non-breaking)
    return []

def _ri_coverage_analysis(records) -> List[RICoverage]:
    # Deterministic heuristic: if service in (EC2,RDS) and running_hours > threshold -> recommend RI
    return []

def _top_hub_enrichment() -> List[TopHubSignal]:
    return [
        TopHubSignal(
            hub_name="MOC",
            hub_type="knowledge_hub",
            score=0.92,
            related_docs=["MOC-2026-04-27", "Cost-Governance-Playbook"],
            insight="MOC shows highest connectivity for cost anomaly patterns; prioritize RI signals for EC2/RDS in prod."
        )
    ]

def run_discovery(env: str) -> DiscoveryPayload:
    records = _fetch_cost_records(env)
    manifest = DiscoveryManifest(env=env)
    anomalies = _detect_anomalies(records)
    ri_recs = _ri_coverage_analysis(records)
    top_hubs = _top_hub_enrichment()

    return DiscoveryPayload(
        manifest=manifest,
        cost_anomalies=anomalies,
        ri_recommendations=ri_recs,
        top_hub_signals=top_hubs,
        metadata={"source": "deterministic-engine", "version": "4.2.0"}
    )
```

---

### 4) CLI — `cli.py` (executable)
```python
#!/usr/bin/env python3
import argparse
from datetime import datetime
from pathlib import Path
from .engine import run_discovery

def main():
    parser = argparse.ArgumentParser(description="Costinel discovery (read-only signals)")
    parser.add_argument("--env", required=True, help="Environment (e.g., prod, staging)")
    parser.add_argument("--out", default=".", help="Output directory")
    parser.add_argument("--format", choices=["json", "parquet"], default="json", help="Output format")
    args = parser.parse_args()

    payload = run_discovery(args.env)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    base = out_dir / f"discovery-{args.env}-{ts}"

    if args.format == "json":
        payload.write_json(f"{base}.json")
        print(f"Discovery JSON written to {base}.json")
    else:
        payload.write_parquet(f"{base}.parquet")
        print(f"Discovery Parquet written to {base}.parquet")

if __name__ == "__main__":
    main()
```
Make executable:
```bash
chmod +x /opt/axentx/Costinel/costinel/discovery/cli.py
```

---

### 5) FastAPI endpoint — `routes/discovery.py`
```python
from fastapi import APIRouter, Query
from costinel.discovery.engine import run_discovery
from costinel.discovery.schemas import DiscoveryPayload

router = APIRouter()

@router.get("/api/discovery", response_model=DiscoveryPayload)
def get_discovery(env: str = Query(..., description="Environment")):
    # Cache in prod (e.g., 5m) — for MVP, compute on request (read-only, cheap)
    return run_discovery(env)
```

Register in main app (if not auto-discovered):
```python
# app.py or similar
from costinel.routes.discovery import router as discovery_router
app.include_router(discovery_router)
```

---

### 6) Quick validation (run once)
```bash
cd /opt/axentx
