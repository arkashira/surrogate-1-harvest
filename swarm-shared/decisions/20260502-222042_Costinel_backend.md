# Costinel / backend

Below is the **single, merged implementation plan** that keeps every strong, non-duplicative idea from both proposals, removes contradictions, and favors **correctness + concrete actionability**.  

Key resolution choices made:
- **Runner determinism**: enforce sorted, stable outputs and explicit provider precedence (AWS → GCP → Azure → multi-cloud) to guarantee reproducibility.  
- **No execution**: strictly read-only/discovery mode (no tagging, no shutdown, no writes).  
- **Hub insight**: prefer live knowledge-graph when present; deterministic, versioned fallback when absent (never crash).  
- **CLI**: Typer with rich console UX + JSON output switch.  
- **Testing**: include a fast offline mock test so CI passes without cloud credentials.  

---

## 1. Project layout
```
Costinel/
├── src/
│   └── discovery/
│       ├── __init__.py
│       ├── cli.py              # Typer entrypoint
│       ├── config.py           # Env/profile resolution
│       ├── runner.py           # Orchestrator (deterministic)
│       ├── providers/
│       │   ├── __init__.py
│       │   ├── base.py         # Provider ABC + Resource/Cost models
│       │   ├── aws.py
│       │   ├── gcp.py
│       │   └── azure.py
│       ├── knowledge/
│       │   ├── __init__.py
│       │   └── hub_insight.py
│       └── utils/
│           ├── __init__.py
│           └── json.py         # Deterministic JSON dumps
├── discovery/                  # Runtime outputs (gitignored)
├── tests/
│   ├── conftest.py
│   └── test_discovery.py
├── pyproject.toml
└── requirements.txt
```

---

## 2. Dependencies (5 min)
```bash
# pyproject.toml or requirements.txt
typer>=0.12
rich>=13
pydantic>=2.5
python-dateutil>=2.8
boto3>=1.26
google-cloud-billing>=1.12
azure-identity>=1.15
azure-mgmt-consumption>=10.0
```

---

## 3. Canonical models (15 min)
```python
# src/discovery/providers/base.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

class CloudProvider(str, Enum):
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"
    MULTI = "multi-cloud"

class Environment(str, Enum):
    PROD = "prod"
    STAGING = "staging"
    DEV = "dev"

class Resource(BaseModel):
    id: str
    name: str
    type: str
    region: str
    account_id: str
    tags: Dict[str, str] = Field(default_factory=dict)
    metadata: Dict = Field(default_factory=dict)

    class Config:
        frozen = True  # immutability for determinism

class CostMetric(BaseModel):
    currency: str = "USD"
    monthly_cost: Decimal
    daily_cost: Decimal
    forecast_30d: Decimal
    last_updated: datetime

    @classmethod
    def from_float(cls, monthly: float, daily: float, forecast: float, last_updated: datetime):
        # Avoid float rounding artifacts in manifests
        return cls(
            monthly_cost=Decimal(str(round(monthly, 2))),
            daily_cost=Decimal(str(round(daily, 2))),
            forecast_30d=Decimal(str(round(forecast, 2))),
            last_updated=last_updated,
        )

class DiscoveryManifest(BaseModel):
    run_id: str
    environment: Environment
    timestamp: datetime
    provider: CloudProvider
    resources: List[Resource]
    cost_summary: CostMetric
    top_hub: Optional[str] = None
    insights: List[str] = Field(default_factory=list)
    audit_trail: Dict[str, str] = Field(default_factory=dict)

    class Config:
        sort_order = True  # stable field order for hashing/diffing
```

---

## 4. Knowledge-RAG hub insight (20 min)
```python
# src/discovery/knowledge/hub_insight.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any

FALLBACK_HUB = {
    "hub_name": "MOC",
    "connection_count": 42,
    "related_docs": [
        "cost-governance-framework.md",
        "multi-cloud-strategy.md",
        "reserved-instance-optimization.md",
    ],
    "context": "Most-connected hub for cost governance patterns",
}

class HubInsightGenerator:
    def __init__(self, knowledge_base_path: Path | str = "knowledge-rag") -> None:
        self.kb_path = Path(knowledge_base_path)
        self.hub_graph_file = self.kb_path / "hub_graph.json"

    def get_top_hub(self) -> Dict[str, Any]:
        try:
            if self.hub_graph_file.is_file():
                with self.hub_graph_file.open() as f:
                    graph = json.load(f)

                hubs: List[Dict] = graph.get("hubs", [])
                if hubs:
                    top = max(hubs, key=lambda h: len(h.get("connections", [])))
                    return {
                        "hub_name": top["name"],
                        "connection_count": len(top.get("connections", [])),
                        "related_docs": top.get("related_docs", [])[:5],
                        "context": top.get("context", FALLBACK_HUB["context"]),
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    }
        except Exception:
            # Never fail discovery due to knowledge graph issues
            pass

        return {
            **FALLBACK_HUB,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
```

---

## 5. Cloud provider adapters (30 min)

### AWS (example; GCP/Azure follow same interface)
```python
# src/discovery/providers/aws.py
from __future__ import annotations

from typing import List
from boto3 import Session
from botocore.exceptions import ClientError

from .base import Resource, CostMetric, CloudProvider

class AWSProvider:
    code = CloudProvider.AWS

    def __init__(self, profile: str | None = None, region: str | None = None) -> None:
        self.session = Session(profile_name=profile, region_name=region)
        self.sts = self.session.client("sts")
        self.account_id = self.sts.get_caller_identity()["Account"]

    def discover_resources(self) -> List[Resource]:
        resources: List[Resource] = []
        ec2 = self.session.client("ec2")

        try:
            paginator = ec2.get_paginator("describe_instances")
            for page in paginator.paginate():
                for reservation in page["Reservations"]:
                    for inst in reservation["Instances"]:
                        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                        resources.append(
                            Resource(
                                id=inst["InstanceId"],
                                name=tags.get("Name", inst["InstanceId"]),
                                type="AWS::EC2::Instance",
                                region=inst["Placement"]["AvailabilityZone"][:-1],
                                account_id=self.account_id,
                                tags=tags,
                                metadata={
                                    "instance_type": inst["InstanceType"],
                                    "state": inst["State"]["Name"],
                                },
                            )
                        )
        except ClientError:
            # Graceful degradation: return partial set
            pass

        # Extend with RDS, S3, etc. here
        return resources

    def get_cost_metrics(self) -> CostMetric:
        ce = self.session.client("ce")
        now = utcnow()
        start = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")

        try:
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": start, "End": end},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
            amount = float(resp["ResultsByTime"][0]["
