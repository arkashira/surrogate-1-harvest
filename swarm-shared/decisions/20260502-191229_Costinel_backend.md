# Costinel / backend

## Final Implementation Plan  
Costinel Discovery Module — ≤2h, deterministic, audit-ready, **Sense + Signal — ไม่ Execute**

---

### Scope & Success Criteria (≤2h)
- CLI: `discovery run` (idempotent, deterministic, JSON-only).
- Inputs: connector configs (AWS/GCP/Azure) + project tree.
- Outputs:
  1. `discovery-manifest.json` — machine-readable, schema-validated, sorted, hashed.
  2. `top-hub-insight.json` — graph hub snapshot (knowledge-rag–driven).
- Zero execution risk: no writes/mutations in cloud; no runtime deployments.
- Deterministic exit codes:  
  0 = clean, 1 = fatal errors, 2 = partial (errors + some results).
- CDN bypass for HF dataset references (no API calls during discovery).

---

### Architecture
```
/opt/axentx/Costinel/
├── src/
│   ├── discovery/
│   │   ├── __init__.py
│   │   ├── runner.py          # CLI: discovery run
│   │   ├── scanners/          # cloud scanners
│   │   │   ├── aws.py
│   │   │   ├── gcp.py
│   │   │   └── azure.py
│   │   ├── project.py         # project tree walker (IaC/services/owners)
│   │   ├── manifest.py        # deterministic manifest builder
│   │   ├── hub_insight.py     # top-hub snapshot (graph + knowledge-rag)
│   │   ├── cdn.py             # HF CDN bypass fetcher
│   │   └── query.py           # lightweight knowledge-rag query helper
├── discovery/
│   ├── config.yaml            # connector configs + tag mappings
│   └── output/                # generated manifests
└── tests/
    └── test_discovery.py
```

---

### Key Code Snippets (integrated, corrected, actionable)

#### `src/discovery/runner.py`
```python
#!/usr/bin/env python3
"""
discovery run — deterministic manifest + top-hub insight
Sense + Signal — ไม่ Execute
"""
import argparse
import json
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

from .scanners import aws, gcp, azure
from .project import walk_project_tree
from .manifest import build_manifest
from .hub_insight import top_hub_snapshot
from .cdn import fetch_cdn_manifest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("discovery")

def run(config_path: Path, project_root: Path, output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()

    # 1) Load config
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        log.error("Config load failed: %s", e)
        return 1

    # 2) Scan connectors
    resources = []
    errors = []

    for conn in cfg.get("connectors", []):
        try:
            if conn["type"] == "aws":
                resources.extend(aws.scan(conn))
            elif conn["type"] == "gcp":
                resources.extend(gcp.scan(conn))
            elif conn["type"] == "azure":
                resources.extend(azure.scan(conn))
            else:
                log.warning("Unsupported connector: %s", conn["type"])
        except Exception as e:
            err = f"{conn['type']}:{conn.get('name','?')} scan failed: {e}"
            log.error(err)
            errors.append(err)

    # 3) Walk project tree (IaC/services/owners)
    try:
        project_items = walk_project_tree(project_root, cfg)
        resources.extend(project_items)
    except Exception as e:
        err = f"Project tree walk failed: {e}"
        log.error(err)
        errors.append(err)

    # 4) Build deterministic manifest
    manifest = build_manifest(resources, cfg, generated_at)
    manifest_path = output_dir / "discovery-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    log.info("Manifest written: %s", manifest_path)

    # 5) Top-hub insight (graph + knowledge-rag)
    try:
        hub = top_hub_snapshot(resources, cfg, generated_at)
        hub_path = output_dir / "top-hub-insight.json"
        hub_path.write_text(json.dumps(hub, indent=2, sort_keys=True))
        log.info("Top-hub insight: %s", hub_path)
    except Exception as e:
        log.warning("Hub snapshot failed (non-fatal): %s", e)
        errors.append(f"hub_snapshot: {e}")

    # 6) CDN bypass: if manifest references HF datasets, fetch file list once
    hf_refs = [r for r in resources if r.get("dataset_hf_repo")]
    for ref in hf_refs:
        try:
            file_list = fetch_cdn_manifest(ref["dataset_hf_repo"], ref.get("dataset_path", ""))
            ref["dataset_files"] = file_list
            ref["dataset_strategy"] = "cdn_bypass"
        except Exception as e:
            log.warning("CDN bypass failed for %s: %s", ref.get("dataset_hf_repo"), e)

    # Rewrite updated manifest if CDN data added
    if hf_refs:
        manifest = build_manifest(resources, cfg, generated_at)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    # Deterministic exit codes
    if errors and not resources:
        return 1
    if errors:
        return 2
    return 0

def main() -> None:
    parser = argparse.ArgumentParser(description="Costinel Discovery Runner")
    parser.add_argument("--config", type=Path, default=Path("discovery/config.yaml"), help="Config path")
    parser.add_argument("--project", type=Path, default=Path("."), help="Project root to walk")
    parser.add_argument("--output", type=Path, default=Path("discovery/output"), help="Output directory")
    args = parser.parse_args()

    code = run(args.config, args.project, args.output)
    sys.exit(code)

if __name__ == "__main__":
    main()
```

---

#### `src/discovery/project.py`
```python
from pathlib import Path
import re

def walk_project_tree(root: Path, cfg):
    """Walk project tree and extract IaC/services/owners/cost centers."""
    root = Path(root).resolve()
    items = []

    # Simple heuristics: detect tf/cdk/pulumi and config files
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in {".tf", ".yaml", ".yml", ".json"}:
            try:
                text = p.read_text(errors="ignore")
            except Exception:
                continue

            # Detect cost center tags in IaC/configs
            ccs = re.findall(r'CostCenter[\s:=]+["\']?([^"\'\s]+)', text, re.IGNORECASE)
            owners = re.findall(r'Owner[\s:=]+["\']?([^"\'\s]+)', text, re.IGNORECASE)

            items.append({
                "source": "project_tree",
                "cloud": "project",
                "account_id": "project",
                "resource_id": str(p.relative_to(root)),
                "service": "IaC" if p.suffix == ".tf" else "config",
                "tags": {
                    "CostCenter": ccs[0] if ccs else "untagged",
                    "Owner": owners[0] if owners else "unknown",
                },
                "path": str(p),
            })

    return items
```

---

#### `src/discovery/manifest.py`
```python
import hashlib

def build_manifest(resources, cfg, generated_at):
    """Deterministic manifest sorted by stable hash id."""
    for r in resources:
        # deterministic id from cloud-native identifiers or project path
        seed = f"{r.get('cloud','project')}:{r.get('account_id','project')}:{r.get('resource_id','?')}"
        r["resource_uid"] = hashlib.sha25
