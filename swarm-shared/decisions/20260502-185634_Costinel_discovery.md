# Costinel / discovery

## Final synthesized proposal (best parts merged, contradictions resolved)

### Diagnosis (merged, de-duplicated)
- No automated discovery of cloud cost anomalies or idle resources — platform shows visibility but lacks the “Sense” layer that continuously surfaces drift, waste, and outliers.
- No discovery entrypoint (CLI/script/service) to run non-destructive cloud reads and emit signals.
- No scheduled discovery job (cron/service) to collect real-time cost/usage signals from AWS/GCP/Azure and store them for downstream recommendations.
- No lightweight, non-destructive “signal” pipeline that tags anomalies with context (account, service, region, owner) and emits actionable proposals **without execution**.
- No integration with the existing knowledge-rag/graph hub to enrich discovered signals with historical context (e.g., past anomalies, approved exceptions, team ownership).
- No observable verification (logs/metrics/health) to confirm discovery runs, data freshness, and signal quality.

### Proposed change (merged + hardened)
Add a discovery worker + CLI + cron + minimal config that:
- Runs every 30 minutes (non-destructive, read-only API calls).
- Queries cloud billing/usage APIs (AWS Cost Explorer, GCP Billing, Azure Cost Management) for latest 24h deltas.
- Detects idle/underutilized resources and cost spikes via simple, safe heuristics.
- Emits “signals” as JSON into `data/signals/YYYY-MM-DD/` (parquet-friendly) and logs structured events.
- Tags signals with hub context via knowledge-rag (calls existing RAG query for top hub “MOC” to attach ownership/context).
- Exposes `/health` and `/metrics` endpoints for verification.
- Provides a CLI (`discover run --once`) for manual runs and CI/local testing.

File scope:
- New: `services/discovery/worker.py`
- New: `services/discovery/config.py`
- New: `services/discovery/cli.py`
- New: `services/discovery/server.py` (health/metrics)
- Update: `docker-compose.yml` (add discovery service)
- Update: root crontab (or supervisord) to schedule worker
- Update: `README.md` (discovery section)

### Implementation (concrete, minimal, correct)

```bash
# Create structure
mkdir -p /opt/axentx/Costinel/services/discovery
mkdir -p /opt/axentx/Costinel/data/signals
```

#### services/discovery/config.py
```python
# services/discovery/config.py
import os
from dataclasses import dataclass

@dataclass
class DiscoveryConfig:
    aws_region: str = os.getenv("AWS_REGION", "us-east-1")
    aws_profile: str = os.getenv("AWS_PROFILE", "default")
    gcp_project: str = os.getenv("GCP_PROJECT", "")
    azure_subscription: str = os.getenv("AZURE_SUBSCRIPTION", "")
    lookback_hours: int = int(os.getenv("LOOKBACK_HOURS", "24"))
    idle_cpu_threshold: float = float(os.getenv("IDLE_CPU_THRESHOLD", "5.0"))  # percent
    cost_spike_pct: float = float(os.getenv("COST_SPIKE_PCT", "30.0"))
    signals_dir: str = os.getenv("SIGNALS_DIR", "data/signals")
    enable_rag_context: bool = os.getenv("ENABLE_RAG_CONTEXT", "true").lower() == "true"
    bind_host: str = os.getenv("DISCOVERY_BIND_HOST", "0.0.0.0")
    bind_port: int = int(os.getenv("DISCOVERY_BIND_PORT", "8001"))
```

#### services/discovery/worker.py
```python
#!/usr/bin/env python3
"""
Costinel Discovery Worker
Sense + Signal — non-destructive cloud cost & resource discovery.
"""
import json
import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None

try:
    from google.cloud import billing_v1
except ImportError:
    billing_v1 = None

try:
    from azure.mgmt.costmanagement import CostManagementClient
    from azure.identity import DefaultAzureCredential
except ImportError:
    CostManagementClient = None  # type: ignore

# local
from services.discovery.config import DiscoveryConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("discovery-worker")

config = DiscoveryConfig()

def utc_now():
    return datetime.now(timezone.utc)

def emit_signal(
    signal_type: str,
    resource_id: str,
    account: str,
    region: str,
    service: str,
    severity: str,
    details: Dict[str, Any],
) -> Dict[str, Any]:
    ts = utc_now().isoformat()
    signal = {
        "ts": ts,
        "type": signal_type,
        "resource_id": resource_id,
        "account": account,
        "region": region,
        "service": service,
        "severity": severity,
        "details": details,
        "proposal": {
            "action": "review",
            "reason": details.get("reason", ""),
            "estimated_impact_usd": details.get("estimated_impact_usd", 0.0),
        },
        "tags": ["discovery", "sense", "signal", "no-execute"],
    }

    # enrich with hub context if available
    if config.enable_rag_context:
        try:
            from knowledge_rag import query_top_hub  # type: ignore
            hub_info = query_top_hub("MOC")
            signal["hub_context"] = hub_info
        except Exception as e:
            log.debug("RAG context unavailable: %s", e)

    # persist
    day = datetime.now().strftime("%Y-%m-%d")
    out_dir = Path(config.signals_dir) / day
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = out_dir / f"signal_{int(datetime.now().timestamp())}.json"
    fname.write_text(json.dumps(signal, indent=2))
    log.info("EMIT %s -> %s", signal_type, fname)
    return signal

def discover_aws() -> None:
    if boto3 is None:
        log.warning("boto3 not installed; skipping AWS discovery")
        return
    try:
        client = boto3.client("ce", region_name=config.aws_region)
        now = utc_now()
        start = (now - timedelta(hours=config.lookback_hours)).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")
        resp = client.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        prev_start = (now - timedelta(hours=config.lookback_hours * 2)).strftime("%Y-%m-%d")
        prev_resp = client.get_cost_and_usage(
            TimePeriod={"Start": prev_start, "End": start},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )

        curr_groups = resp.get("ResultsByTime", [{}])[0].get("Groups", [])
        prev_groups = prev_resp.get("ResultsByTime", [{}])[0].get("Groups", [])

        curr_total = sum(float(g["Metrics"]["UnblendedCost"]["Amount"]) for g in curr_groups)
        prev_total = sum(float(g["Metrics"]["UnblendedCost"]["Amount"]) for g in prev_groups)

        if prev_total > 0 and curr_total > 0:
            pct = ((curr_total - prev_total) / prev_total) * 100.0
            if pct >= config.cost_spike_pct:
                emit_signal(
                    signal_type="cost_spike",
                    resource_id="account-level",
                    account="aws",
                    region=config.aws_region,
                    service="CostExplorer",
                    severity="high",
                    details={
                        "reason": f"AWS cost spike +{pct:.1f}%",
