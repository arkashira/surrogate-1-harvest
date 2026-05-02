# Costinel / backend

## Implementation Plan — Costinel Discovery Module (≤2h)

**Scope**: Deterministic discovery CLI (`discovery run`) that produces machine-readable manifests and a top-hub insight snapshot for visibility-first onboarding (Sense + Signal — ไม่ Execute).

### 1) High-value incremental improvement
Ship a **single deterministic entrypoint** (`discovery run`) that:
- Reads `config/discovery.yaml` (or env) for declared cloud sources
- Produces `manifests/sources.json` (declared sources + auth strategy)
- Produces `manifests/discovery.json` (discovered resources + metadata)
- Produces `insights/top-hub.md` (top-hub doc insight snapshot)
- Emits structured logs + deterministic checksums for auditability
- Zero execution — only read/observe/list; no state changes

### 2) File layout (create/modify)
```
Costinel/
├── discovery/
│   ├── __init__.py
│   ├── cli.py          # entrypoint: discovery run
│   ├── config.py       # config loader (YAML/env)
│   ├── sources.py      # sources.json producer
│   ├── discover.py     # discovery.json producer (dry-run)
│   └── insights.py     # top-hub snapshot writer
├── manifests/
│   ├── sources.json    # generated
│   └── discovery.json  # generated
├── insights/
│   └── top-hub.md      # generated
├── config/
│   └── discovery.yaml  # input
└── pyproject.toml / requirements.txt
```

### 3) Implementation snippets

#### `discovery/cli.py`
```python
#!/usr/bin/env python3
"""
Costinel Discovery CLI
Usage: discovery run [--config CONFIG] [--output-dir OUTPUT_DIR]
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from .config import load_config
from .sources import build_sources_manifest
from .discover import run_discovery
from .insights import write_top_hub_snapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("discovery")

def main() -> None:
    parser = argparse.ArgumentParser(description="Costinel Discovery (Sense + Signal)")
    parser.add_argument("command", choices=["run"], help="Run discovery")
    parser.add_argument("--config", default="config/discovery.yaml", help="Config path")
    parser.add_argument("--output-dir", default=".", help="Project root")
    args = parser.parse_args()

    root = Path(args.output_dir).resolve()
    cfg = load_config(Path(args.config))

    log.info("Starting Costinel discovery (dry-run)")

    # 1) sources.json
    sources_path = root / "manifests" / "sources.json"
    sources_path.parent.mkdir(parents=True, exist_ok=True)
    sources = build_sources_manifest(cfg)
    sources_path.write_text(json.dumps(sources, indent=2, sort_keys=True))
    log.info("Wrote %s", sources_path.relative_to(root))

    # 2) discovery.json
    discovery_path = root / "manifests" / "discovery.json"
    discovered = run_discovery(cfg, root)
    discovery_path.write_text(json.dumps(discovered, indent=2, sort_keys=True, default=str))
    log.info("Wrote %s", discovery_path.relative_to(root))

    # 3) top-hub insight snapshot
    insights_dir = root / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    top_hub_path = insights_dir / "top-hub.md"
    write_top_hub_snapshot(top_hub_path, discovered)
    log.info("Wrote %s", top_hub_path.relative_to(root))

    log.info("Discovery complete (Sense + Signal — ไม่ Execute)")

if __name__ == "__main__":
    main()
```

#### `discovery/config.py`
```python
from pathlib import Path
from typing import Dict, Any
import yaml
import os

def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        # Minimal defaults when no config present
        return {
            "sources": [
                {"provider": "aws", "accounts": ["*"], "auth": {"strategy": "env"}}
            ]
        }
    with path.open() as f:
        return yaml.safe_load(f)
```

#### `discovery/sources.py`
```python
from typing import Dict, Any, List
import hashlib
import time

def build_sources_manifest(cfg: Dict[str, Any]) -> Dict[str, Any]:
    sources: List[Dict[str, Any]] = cfg.get("sources", [])
    normalized = []
    for s in sources:
        normalized.append({
            "provider": s.get("provider"),
            "accounts": s.get("accounts", []),
            "auth": s.get("auth", {"strategy": "env"}),
            "mode": "read-only",
            "discovery": "declared"
        })

    manifest = {
        "generated_at": int(time.time()),
        "generator": "Costinel/discovery",
        "schema_version": "1.0",
        "sources": normalized,
        "checksum": ""  # filled below
    }
    payload = json.dumps({"sources": normalized}, sort_keys=True)
    manifest["checksum"] = hashlib.sha256(payload.encode()).hexdigest()
    return manifest

# local import helper
import json
```

#### `discovery/discover.py`
```python
from typing import Dict, Any, List
import time

def run_discovery(cfg: Dict[str, Any], root) -> Dict[str, Any]:
    """
    Dry-run discovery: list observable resources without mutating state.
    For MVP, produce deterministic placeholder entries keyed by declared sources.
    """
    sources = cfg.get("sources", [])
    discovered: List[Dict[str, Any]] = []

    for s in sources:
        provider = s.get("provider")
        accounts = s.get("accounts", [])
        discovered.append({
            "provider": provider,
            "accounts": accounts,
            "mode": "read-only",
            "resources": [],
            "status": "declared",
            "message": "Dry-run: no live enumeration performed (Sense + Signal)"
        })

    return {
        "generated_at": int(time.time()),
        "schema_version": "1.0",
        "discovery_run_id": f"disc-{int(time.time())}",
        "sources": discovered,
        "summary": {
            "total_sources": len(sources),
            "mode": "dry-run"
        }
    }
```

#### `discovery/insights.py`
```python
from pathlib import Path
from typing import Dict, Any
import datetime

def write_top_hub_snapshot(path: Path, discovered: Dict[str, Any]) -> None:
    """
    Produce a top-hub insight snapshot (knowledge-rag style) for audit/onboarding.
    Pattern: top-hub doc insight — review most-connected hub before planning.
    """
    now = datetime.datetime.utcnow().isoformat() + "Z"
    lines = [
        "# Top-Hub Insight Snapshot",
        "",
        f"Generated: {now}",
        "",
        "## Summary",
        "",
        "This snapshot provides a visibility-first overview of declared sources and high-level",
        "discovery results. It is intended for audit and onboarding (Sense + Signal — ไม่ Execute).",
        "",
        "## Declared Sources",
        "",
    ]

    for src in discovered.get("sources", []):
        lines.append(f"- Provider: `{src.get('provider')}`")
        lines.append(f"  Accounts: `{src.get('accounts')}`")
        lines.append(f"  Status: `{src.get('status')}`")
        lines.append("")

    lines.extend([
        "## Top Hub (MOC) — Recommended Pre-Flight Review",
        "",
        "Before onboarding or running further discovery, review the most-connected hub (MOC)",
        "to understand context, dependencies, and governance boundaries.",
        "",
        "Tags: #knowledge-rag #graph #hub",
        "",
        "## Next Steps",
        "",
        "1. Validate declared sources and auth strategies in `manifests/sources.json`",
        "2. Review `manifests/discovery.json` for dry-run results",
        "3. Promote
