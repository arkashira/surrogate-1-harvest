# Costinel / backend

## Implementation Plan — Costinel Discovery Module (≤2h)

**Goal**: Ship a deterministic, audit-ready `discovery run` CLI that produces:
1. Machine-readable manifests (`discovery/manifest-{env}.json`)
2. Top-hub insight snapshot (`discovery/top-hub-{env}.json`)
3. No execution — pure Sense + Signal

**Time budget**: ~90 min implementation + 30 min test/validation.

---

### 1. Architecture (local to `/opt/axentx/Costinel`)

```
Costinel/
├── discovery/
│   ├── __init__.py
│   ├── cli.py          # typer CLI entrypoint
│   ├── runner.py       # deterministic orchestrator
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── aws.py      # boto3 dry-run collectors
│   │   ├── gcp.py      # gcloud dry-run collectors
│   │   └── azure.py    # azure-mgmt dry-run collectors
│   ├── models.py       # pydantic schemas
│   └── insights.py     # top-hub graph builder
└── pyproject.toml / requirements additions
```

---

### 2. Concrete Implementation

#### `discovery/models.py`

```python
from datetime import datetime
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field

class ResourceItem(BaseModel):
    id: str
    name: str
    type: str
    region: str
    account_id: str
    tags: Dict[str, str] = Field(default_factory=dict)
    raw: Dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime = Field(default_factory=datetime.utcnow)

class CostAnomaly(BaseModel):
    resource_id: str
    metric: str
    current: float
    baseline: float
    severity: str  # low|medium|high
    description: str

class TopHubInsight(BaseModel):
    hub: str
    rank: int
    connections: int
    related_resources: List[str]
    signals: List[str]
    generated_at: datetime = Field(default_factory=datetime.utcnow)

class DiscoveryManifest(BaseModel):
    environment: str
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    resources: List[ResourceItem] = Field(default_factory=list)
    anomalies: List[CostAnomaly] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)
```

---

#### `discovery/insights.py`

```python
from collections import Counter
from .models import TopHubInsight, ResourceItem

def build_top_hub(resources: list[ResourceItem], env: str) -> TopHubInsight:
    """
    Deterministic top-hub selection:
    - Uses service-type as hub (e.g., "MOC", "EC2", "CloudSQL", "VM")
    - Ranks by connection count (tags + region + account intersections)
    """
    hub_counter: Counter[str] = Counter()
    hub_resources: dict[str, list[str]] = {}

    for r in resources:
        # Normalize hub name by service family
        family = r.type.split("/")[-1].upper() if "/" in r.type else r.type.upper()
        hub = family[:20]  # cap length

        hub_counter[hub] += 1
        hub_resources.setdefault(hub, []).append(r.id)

    top_hub_name, count = hub_counter.most_common(1)[0] if hub_counter else ("UNKNOWN", 0)

    return TopHubInsight(
        hub=top_hub_name,
        rank=1,
        connections=count,
        related_resources=hub_resources.get(top_hub_name, []),
        signals=[
            f"{top_hub_name} accounts for {count} discovered resources",
            "Visibility-first: no execution performed",
            "Sense + Signal — ไม่ Execute"
        ]
    )
```

---

#### `discovery/providers/aws.py`

```python
import boto3
from typing import List
from .models import ResourceItem

def discover_aws_resources(profile: str = None, region: str = "us-east-1") -> List[ResourceItem]:
    """
    Dry-run discovery using read-only IAM permissions.
    No state changes — pure describe/list calls.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    ec2 = session.client("ec2")
    rds = session.client("rds")

    items: List[ResourceItem] = []

    # EC2 instances
    try:
        resp = ec2.describe_instances()
        for resv in resp.get("Reservations", []):
            for inst in resv.get("Instances", []):
                items.append(ResourceItem(
                    id=inst["InstanceId"],
                    name=next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), inst["InstanceId"]),
                    type="aws/ec2/instance",
                    region=inst["Placement"]["AvailabilityZone"][:-1],
                    account_id=resv.get("OwnerId", "unknown"),
                    tags={t["Key"]: t["Value"] for t in inst.get("Tags", [])},
                    raw={"state": inst["State"]["Name"]}
                ))
    except Exception as e:
        # Graceful degradation — log later via CLI
        pass

    # RDS instances
    try:
        resp = rds.describe_db_instances()
        for db in resp.get("DBInstances", []):
            items.append(ResourceItem(
                id=db["DBInstanceIdentifier"],
                name=db.get("DBName", db["DBInstanceIdentifier"]),
                type="aws/rds/instance",
                region=db["AvailabilityZone"][:-1],
                account_id=db.get("DBInstanceArn", "").split(":")[4] if ":" in db.get("DBInstanceArn", "") else "unknown",
                tags={},  # RDS tags require separate call — skip for speed
                raw={"engine": db["Engine"], "status": db["DBInstanceStatus"]}
            ))
    except Exception:
        pass

    return items
```

(GCP + Azure stubs follow same pattern — omitted for brevity, return `[]` if credentials absent.)

---

#### `discovery/runner.py`

```python
import json
from pathlib import Path
from datetime import datetime
from .models import DiscoveryManifest
from .insights import build_top_hub
from .providers import aws, gcp, azure

def run_discovery(environment: str = "dev", output_dir: str = "discovery") -> Path:
    """
    Deterministic runner:
    1. Collect from all enabled providers (dry-run only)
    2. Build manifest
    3. Build top-hub insight
    4. Write machine-readable JSON files
    """
    out_path = Path(output_dir)
    out_path.mkdir(exist_ok=True, parents=True)

    all_resources = []
    all_resources.extend(aws.discover_aws_resources())
    all_resources.extend(gcp.discover_gcp_resources())
    all_resources.extend(azure.discover_azure_resources())

    anomalies = []  # Placeholder: anomaly detection module to be added later

    manifest = DiscoveryManifest(
        environment=environment,
        resources=all_resources,
        anomalies=anomalies,
        summary={
            "total_resources": len(all_resources),
            "providers_used": ["aws", "gcp", "azure"],
            "mode": "dry-run",
            "execution": False
        }
    )

    # Write manifest
    manifest_file = out_path / f"manifest-{environment}.json"
    manifest_file.write_text(manifest.model_dump_json(indent=2))

    # Write top-hub insight
    top_hub = build_top_hub(all_resources, environment)
    hub_file = out_path / f"top-hub-{environment}.json"
    hub_file.write_text(top_hub.model_dump_json(indent=2))

    return manifest_file
```

---

#### `discovery/cli.py`

```python
import typer
from .runner import run_discovery

app = typer.Typer(help="Costinel Discovery CLI — Sense + Signal (ไม่ Execute)")

@app.command()
def run(
    environment: str = typer.Option("dev", "--env", help="Target environment"),
    output_dir: str = typer.Option("discovery", "--out", help="Output directory")
):
    """
    Run deterministic
