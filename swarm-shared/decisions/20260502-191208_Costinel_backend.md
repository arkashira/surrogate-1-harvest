# Costinel / backend

## Final Implementation Plan — Costinel Discovery Module (≤2h)

**Objective**: Ship a deterministic, audit-ready CLI (`discovery run`) that produces machine-readable manifests and top-hub insight snapshots with **zero execution risk** (Sense + Signal only).

---

### Core Design Decisions (Resolved Contradictions)

| Decision | Candidate 1 | Candidate 2 | Final Choice | Rationale |
|----------|-------------|-------------|--------------|-----------|
| **CLI Framework** | Click | Typer | **Click** | More explicit control for deterministic behavior; easier to enforce sorted JSON output and audit logging inline. |
| **Cloud Scanning** | Explicit read-only API calls (CE, Asset Inventory, Cost Mgmt) | Abstract connector interface | **Hybrid**: concrete read-only scanners + connector registry | Ensures immediate correctness while allowing future extensibility. |
| **Output Location** | `output/{manifests,insights}/` | `output/discovery-{ts}.json` + `output/top-hub-{ts}.json` | **Structured subdirs** (`manifests/`, `insights/`) | Improves discoverability and auditability; matches Candidate 1’s clarity. |
| **Schema Validation** | Inline `jsonschema` minimal check | Pydantic models | **Both**: Pydantic for runtime safety + inline schema for file-level validation | Pydantic ensures correct types; JSON Schema validates persisted files. |
| **Top-Hub Insight** | Direct `query_top_hub_insight()` call | Same | **Same** | Reuse existing knowledge-rag pattern; no contradiction. |
| **Audit Trail** | JSON lines in `audit/discovery-audit.log` | Same | **Same** | Critical for compliance; deterministic checksum included. |
| **Dry-Run** | `--dry-run` flag with mock data | Same | **Same** | Enables fast CI/local validation without cloud calls. |

---

### Implementation Plan (≤2h)

#### 1. Project Structure
```
src/costinel/
├── cli/
│   └── discovery.py          # Click entrypoint
├── discovery/
│   ├── __init__.py
│   ├── inventory.py          # Read-only scanners (AWS/GCP/Azure)
│   ├── models.py             # Pydantic schemas
│   └── registry.py           # Connector registry
├── knowledge/
│   └── rag.py                # query_top_hub_insight()
├── audit.py                  # log_discovery_action()
└── __init__.py
```

#### 2. Deterministic CLI Entrypoint (`src/costinel/cli/discovery.py`)
- Uses Click for explicit control.
- Enforces sorted JSON output (`sort_keys=True`).
- Generates ISO timestamps and deterministic filenames.
- Emits audit log entry with SHA-256 manifest checksum.

#### 3. Read-Only Cloud Inventory Scanner (`src/costinel/discovery/inventory.py`)
- **AWS**: Cost Explorer (ce:GetCostAndUsage) + Resource Groups Tagging (tag:GetResources) — read-only.
- **GCP**: Cloud Asset Inventory (cloudasset:SearchAllResources) + Cloud Billing Catalog — read-only.
- **Azure**: Cost Management Query (Microsoft.CostManagement/query) + Resource Graph (resources) — read-only.
- **Normalization**: Canonical schema:
  ```json
  {
    "id": "string",
    "type": "string",
    "cloud": "aws|gcp|azure",
    "account": "string",
    "region": "string",
    "tags": {"key": "value"},
    "cost_last_30d": "number",
    "currency": "USD",
    "discovered_at": "ISO8601"
  }
  ```

#### 4. Machine-Readable Manifest
- Saved to `manifests/discovery-{YYYYMMDD-HHMMSS}.json`.
- Includes schema version, generation timestamp, summary stats (total resources, projected monthly cost, coverage by service/region), and sorted resource list.

#### 5. Top-Hub Insight Snapshot
- Invokes `knowledge.rag.query_top_hub_insight()` after inventory.
- Saves to `insights/top-hub-{YYYYMMDD-HHMMSS}.json`:
  ```json
  {
    "generated_at": "ISO8601",
    "top_hub": "MOC",
    "related_docs": ["doc1.md", "doc2.md"],
    "contextual_insights": ["..."]
  }
  ```

#### 6. Audit Trail
- Appends JSON line to `audit/discovery-audit.log`:
  ```json
  {
    "action": "discovery_run",
    "timestamp": "ISO8601",
    "user": "system|CI",
    "manifest_path": "...",
    "manifest_checksum": "sha256",
    "resources_count": 123
  }
  ```

#### 7. Validation & Tests
- `--dry-run` flag: validates schema using Pydantic + JSON Schema without external calls.
- Inline JSON Schema validation on written files.
- Smoke test: `discovery run --dry-run --output-dir ./test-output`.

#### 8. Docker & Entrypoint
- Expose via `python -m costinel.cli.discovery`.
- Optional `docker-compose.yml` service for isolated runs.

---

### Code Snippets

#### `src/costinel/cli/discovery.py`
```python
#!/usr/bin/env python3
import json
import hashlib
import datetime
from pathlib import Path

import click

from costinel.discovery.inventory import scan_cloud_inventory
from costinel.knowledge.rag import query_top_hub_insight
from costinel.audit import log_discovery_action
from costinel.discovery.models import DiscoveryManifest, TopHubInsight


def _iso_now():
    return datetime.datetime.utcnow().isoformat() + "Z"


def _deterministic_filename(base, ext):
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"{base}-{ts}.{ext}"


def _save_json(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)


def _manifest_checksum(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


@click.group()
def cli():
    pass


@cli.command()
@click.option("--output-dir", default="output", help="Output directory")
@click.option("--cloud-connector", multiple=True, help="Connectors (aws|gcp|azure)")
@click.option("--dry-run", is_flag=True, help="Validate without external calls")
def run(output_dir, cloud_connector, dry_run):
    """Run deterministic discovery (Sense + Signal)."""
    output_dir = Path(output_dir)
    manifests_dir = output_dir / "manifests"
    insights_dir = output_dir / "insights"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    insights_dir.mkdir(parents=True, exist_ok=True)

    timestamp = _iso_now()
    manifest_path = manifests_dir / _deterministic_filename("discovery", "json")
    insight_path = insights_dir / _deterministic_filename("top-hub", "json")

    # Inventory scan (Sense only)
    if dry_run:
        inventory = []
        summary = {"resources": 0, "projected_monthly_cost_usd": 0, "services": {}}
    else:
        inventory, summary = scan_cloud_inventory(connectors=list(cloud_connector) or None)

    # Build and validate manifest
    manifest_data = {
        "schema_version": "1.0.0",
        "generated_at": timestamp,
        "generated_by": "costinel-discovery",
        "summary": summary,
        "resources": sorted(inventory, key=lambda r: (r["cloud"], r["account"], r["id"])),
    }
    manifest = DiscoveryManifest(**manifest_data)
    _save_json(manifest.dict(exclude_none=True, sort_keys=True), manifest_path)

    # Top-hub insight (Signal)
    hub_data = query_top_hub_insight()
    insight = TopHubInsight(
        generated_at=timestamp,
        top_hub=hub_data.get("hub"),
        related_docs=hub_data.get("related_docs", []),
        contextual_insights=hub_data.get("ins
