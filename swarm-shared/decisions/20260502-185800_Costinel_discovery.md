# Costinel / discovery

## Highest-Value Incremental Improvement (<2h)

**Add a lightweight discovery surface** that exposes:
1. Machine-readable manifest of ingestible sources (cloud accounts, datasets, schemas)
2. Top-hub knowledge context (from RAG/graph) for onboarding decisions
3. File manifest for HF datasets to enable CDN-only training (bypass API rate limits)

This unblocks onboarding, RAG queries, and surrogate-1 training by providing the "sense" layer Costinel needs without touching execution.

---

## Implementation Plan

### 1. Create `/opt/axentx/Costinel/discovery/` module (15 min)
- `manifest.py` — generates machine-readable source manifest
- `knowledge.py` — queries top-hub docs via RAG
- `hf_files.py` — lists HF dataset files for CDN bypass
- `__init__.py` — exposes CLI and API

### 2. Add CLI entrypoint (10 min)
- `python -m costinel.discovery manifest` → outputs JSON/YAML
- `python -m costinel.discovery knowledge --hub MOC` → returns top-hub insights
- `python -m costinel.discovery hf-files --repo <repo> --date <YYYY-MM-DD>` → saves file-list JSON for training scripts

### 3. Add health/probe endpoint stub (10 min)
- `GET /health` and `GET /ready` returning manifest summary

### 4. Update README with discovery commands (5 min)

---

## Code Snippets

### `/opt/axentx/Costinel/costinel/discovery/manifest.py`
```python
"""
Generate machine-readable manifest of ingestible sources.
Used by onboarding, training pipelines, and health probes.
"""
from __future__ import annotations
import json
import yaml
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

@dataclass
class CloudAccount:
    provider: str  # "aws", "gcp", "azure"
    account_id: str
    name: str
    region: str
    enabled: bool = True
    last_sync: Optional[str] = None

@dataclass
class DatasetSource:
    repo: str  # "datasets/company/cost-data"
    path: str  # "batches/mirror-merged/2026-04-29/"
    format: str  # "parquet"
    schema_hash: str
    file_count: int
    size_bytes: int
    cdn_manifest: Optional[str] = None  # path to file-list.json

@dataclass
class Manifest:
    version: str
    generated_at: str
    cloud_accounts: List[CloudAccount]
    datasets: List[DatasetSource]
    knowledge_hubs: Dict[str, str]  # hub_name -> top insight

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def to_yaml(self) -> str:
        return yaml.dump(asdict(self), sort_keys=False)

def discover_cloud_accounts() -> List[CloudAccount]:
    """Placeholder: integrate with AWS/GCP/Azure SDKs or config."""
    # In practice, read from ~/.aws/credentials, env vars, or config file
    return [
        CloudAccount(
            provider="aws",
            account_id="123456789012",
            name="prod-main",
            region="us-east-1",
            enabled=True,
            last_sync=datetime.utcnow().isoformat() + "Z",
        ),
        CloudAccount(
            provider="aws",
            account_id="987654321098",
            name="staging-analytics",
            region="eu-west-1",
            enabled=True,
            last_sync=None,
        ),
    ]

def discover_datasets(base_path: Path = Path("/opt/axentx/data")) -> List[DatasetSource]:
    """Discover parquet datasets for surrogate-1 training."""
    datasets = []
    for date_dir in sorted(base_path.glob("mirror-merged/*")):
        if not date_dir.is_dir():
            continue
        parquet_files = list(date_dir.glob("*.parquet"))
        if not parquet_files:
            continue
        total_size = sum(f.stat().st_size for f in parquet_files)
        datasets.append(
            DatasetSource(
                repo="datasets/axentx/costinel-cost-data",
                path=str(date_dir.relative_to(base_path)),
                format="parquet",
                schema_hash="sha256:" + "0" * 64,  # placeholder
                file_count=len(parquet_files),
                size_bytes=total_size,
                cdn_manifest=str(date_dir / "file-list.json"),
            )
        )
    return datasets

def build_manifest() -> Manifest:
    return Manifest(
        version="4.2.0",
        generated_at=datetime.utcnow().isoformat() + "Z",
        cloud_accounts=discover_cloud_accounts(),
        datasets=discover_datasets(),
        knowledge_hubs={},  # populated by knowledge.py
    )

if __name__ == "__main__":
    manifest = build_manifest()
    print(manifest.to_json())
```

### `/opt/axentx/Costinel/costinel/discovery/knowledge.py`
```python
"""
Query top-hub RAG insights for contextual onboarding decisions.
Follows pattern: review most-connected hub (e.g., MOC) before planning.
"""
import json
from pathlib import Path
from typing import Dict, List, Optional

def get_top_hub_insight(hub_name: str = "MOC") -> Dict:
    """
    Return top insight for a knowledge hub.
    In production, this queries a graph DB or RAG index.
    For now, returns curated static insight based on known patterns.
    """
    insights = {
        "MOC": {
            "hub": "MOC",
            "connections": 42,
            "top_insight": (
                "Cost governance decisions require pre-execution signals "
                "only — no direct execute permissions. All changes must "
                "flow through proposal → human review → change management."
            ),
            "tags": ["#knowledge-rag", "#graph", "#hub"],
            "last_updated": "2026-04-27",
        },
        "surrogate-1": {
            "hub": "surrogate-1",
            "connections": 28,
            "top_insight": (
                "Use CDN bypass for HF dataset training: download via "
                "resolve/main/ URLs to avoid API rate limits. Pre-list "
                "files once, embed list in training script for zero-API "
                "data loading during Lightning training."
            ),
            "tags": ["#training", "#huggingface", "#cdn", "#rate-limit-bypass"],
            "last_updated": "2026-04-29",
        },
    }
    return insights.get(hub_name, {"error": f"Hub {hub_name} not found"})

def list_hubs() -> List[Dict]:
    return [
        {"name": "MOC", "connections": 42, "description": "Cost governance decision hub"},
        {"name": "surrogate-1", "connections": 28, "description": "Training pipeline patterns"},
    ]

if __name__ == "__main__":
    import sys
    hub = sys.argv[1] if len(sys.argv) > 1 else "MOC"
    print(json.dumps(get_top_hub_insight(hub), indent=2))
```

### `/opt/axentx/Costinel/costinel/discovery/hf_files.py`
```python
"""
List HF dataset files for CDN bypass training.
Generates file-list.json to embed in training scripts.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    list_repo_tree = None

def list_hf_files(repo: str, date_folder: str, token: Optional[str] = None) -> Dict:
    """
    List files in a date folder of an HF dataset repo.
    Returns dict suitable for CDN-only training.
    """
    if list_repo_tree is None:
        return {"error": "huggingface_hub not installed"}

    try:
        tree = list_repo_tree(
            repo_id=repo,
            path=date_folder,
            recursive=False,
            token
