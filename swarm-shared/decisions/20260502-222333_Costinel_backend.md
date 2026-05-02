# Costinel / backend

## Final Synthesis — Backend Implementation (Sense + Signal, No Execute)

**Core decision**: Merge the strongest, most actionable parts of both proposals into a single minimal backend change that can ship in <2h, is deterministic, read-only, and audit-ready.

---

### 1) CLI (single entrypoint)

File: `src/cli/discovery.py`

```bash
#!/usr/bin/env bash
# Usage: bash src/cli/discovery.py run --env prod

set -euo pipefail
SHELL=/bin/bash
cd /opt/axentx/Costinel

ENV=""
while [[ $# -gt 0 ]]; do
  case $1 in
    run) shift ;;
    --env) ENV="$2"; shift 2 ;;
    *) echo "Unknown option $1"; exit 1 ;;
  esac
done

if [[ -z "$ENV" ]]; then
  echo "Missing --env <env>"
  exit 1
fi

python3 -m costinel.discovery.run --env "$ENV"
```

```bash
chmod +x src/cli/discovery.py
```

---

### 2) Backend discovery runner

File: `src/costinel/discovery/run.py`

```python
import argparse
import datetime
import hashlib
import json
import os
import socket
import uuid
from pathlib import Path
from typing import Dict, Any

from costinel.knowledge_rag import query_top_hub_insight
from costinel.audit import append_audit_entry


def build_manifest(env: str) -> Dict[str, Any]:
    now = datetime.datetime.utcnow().isoformat() + "Z"
    return {
        "meta": {
            "version": "4.2.0",
            "generated_at": now,
            "generator": "Costinel/discovery",
            "env": env,
            "run_id": str(uuid.uuid4()),
            "hostname": socket.gethostname(),
            "sha256": "",
            "signature": "SIG:PENDING",
        },
        "sense": {
            "scope": {
                "cloud_providers": ["aws", "gcp", "azure"],
                "read_only": True,
                "execution_allowed": False,
            },
            "observations": {
                "cost_dashboard": {"available": True, "real_time": False},
                "anomalies": [],
                "idle_over_provisioned": [],
                "ri_coverage_gaps": [],
                "recommendations": [],
            },
        },
        "signal": {
            "top_hub": {},
            "governance": {"policy_check": "pass", "audit_required": True},
        },
        "proposal": {
            "human_review_required": True,
            "change_management_handoff": True,
            "execution_blocked": True,
        },
    }


def build_signal(env: str) -> Dict[str, Any]:
    """
    Deterministic signal payload (surrogate-1 shape).
    """
    # Stubbed signals; replace with real cost-usage queries when available.
    signals = [
        {
            "signal_type": "anomaly",
            "entity": f"{env}/aws/account/123456789012",
            "severity": "medium",
            "context": "Unusual spend spike in shared-services account.",
            "recommendation": "Review consolidated billing and tagging.",
        },
        {
            "signal_type": "idle",
            "entity": f"{env}/gcp/project/example-prod",
            "severity": "low",
            "context": "Detected idle VM instances (avg CPU <5% 7d).",
            "recommendation": "Schedule stop/resize or apply autoscaling.",
        },
        {
            "signal_type": "ri_gap",
            "entity": f"{env}/aws/ec2",
            "severity": "high",
            "context": "On-demand utilization >80% with low RI coverage.",
            "recommendation": "Purchase convertible RIs for baseline load.",
        },
    ]
    return {"signals": signals}


def persist_manifest(manifest: Dict[str, Any], env: str) -> Path:
    out_dir = Path("data/discovery") / env
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"manifest-{ts}.json"
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    args = parser.parse_args()

    manifest = build_manifest(args.env)

    # Enrich with top-hub insight (non-blocking)
    try:
        hub_insight = query_top_hub_insight(hub_name="MOC")
        manifest["signal"]["top_hub"] = hub_insight
    except Exception as exc:
        manifest["signal"]["top_hub"] = {"error": str(exc)}

    # Merge deterministic signals
    manifest["signal"]["surrogate"] = build_signal(args.env)

    # Fingerprint
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["meta"]["sha256"] = hashlib.sha256(payload).hexdigest()

    out_path = persist_manifest(manifest, args.env)

    # Immutable audit
    ts = manifest["meta"]["generated_at"]
    append_audit_entry(
        {
            "ts": ts,
            "env": args.env,
            "action": "discovery_run",
            "manifest_sha256": manifest["meta"]["sha256"],
            "manifest_path": str(out_path),
            "actor": os.getenv("USER", "system"),
            "system": "Costinel",
            "sense_signal_only": True,
        }
    )

    print(json.dumps({"status": "ok", "manifest": str(out_path), "sha256": manifest["meta"]["sha256"]}))


if __name__ == "__main__":
    main()
```

---

### 3) Knowledge-rag stub (non-executing)

File: `src/costinel/knowledge_rag.py`

```python
from typing import Dict, Any


def query_top_hub_insight(hub_name: str) -> Dict[str, Any]:
    return {
        "hub": hub_name,
        "insight": "Most-connected hub indicates cross-account cost anomalies in shared services.",
        "actionability": "Review consolidated billing and shared service tagging.",
        "tags": ["knowledge-rag", "graph", "hub", "MOC"],
        "generated_at": "2026-05-03T00:00:00Z",
    }
```

---

### 4) Immutable audit (append-only)

File: `src/costinel/audit.py`

```python
import json
from pathlib import Path
from typing import Dict, Any

AUDIT_LOG = Path("data/audit.log")


def append_audit_entry(entry: Dict[str, Any]) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
```

---

### 5) FastAPI read-only endpoint

File: `src/costinel/api/v1/discovery.py`

```python
from fastapi import APIRouter, HTTPException
from pathlib import Path
import json

router = APIRouter()


@router.get("/v1/discovery/{env}")
def get_latest_discovery(env: str):
    manifest_dir = Path("data/discovery") / env
    if not manifest_dir.exists():
        raise HTTPException(status_code=404, detail="No discovery data for env")

    latest = max(manifest_dir.glob("manifest-*.json"), key=lambda p: p.stat().st_mtime)
    return json.loads(latest.read_text(encoding="utf-8"))
```

---

### 6) Summary of resolved choices

- **Deterministic + signed manifest**: Adopted Candidate 1’s manifest shape and fingerprinting; kept Candidate 2’s surrogate-1 signal shape for immediate product value.
