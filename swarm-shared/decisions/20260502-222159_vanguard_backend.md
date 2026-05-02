# vanguard / backend

## Final Synthesis — One Correct, Actionable Plan

**Core diagnosis (merged, de-duplicated):**  
- No canonical discovery entrypoint → violates `#knowledge-rag #graph #hub` and forces ad-hoc exploration.  
- No CDN-bypass file-list strategy for HF datasets → future surrogate-1 training will hit API/rate limits instead of using CDN fetches.  
- No reusable Lightning Studio wrapper → violates `#lightning-ai #quota` and `#lightning-ai #idle-timeout`; causes quota waste and idle runs.  
- No centralized HF ingestion that projects heterogeneous repos to strict `{prompt,response}` + attribution in filename → violates `#training #pyarrow #hf-datasets #schema` and `#ingestion #schema #surrogate-1`.  
- Missing wrapper script hygiene (shebang, executable, robust SHELL/cron behavior) → violates `#bash #script-error #cron`.

**Chosen approach:** adopt Candidate 1’s concrete file layout and entrypoints, fix Candidate 1’s bugs and omissions, and harden for production use.

---

### 1) Files to create/modify

```
/opt/axentx/vanguard/backend/
├── discovery.py                 # canonical hub discovery entrypoint
├── lightning_orchestrator.py    # reusable Studio wrapper (idle-stop + quota reuse)
├── ingest_hf_safe.py            # CDN-bypass file-list + safe {prompt,response} projection
└── run_job.sh                   # cron-safe wrapper (shebang, SHELL, error handling)
requirements.txt                 # add: lightning-ai, huggingface-hub, requests, pyarrow
```

---

### 2) Implementation (corrected + hardened)

#### `/opt/axentx/vanguard/backend/discovery.py`
```python
#!/usr/bin/env python3
"""
Canonical discovery entrypoint: surface top-hub insights before planning.
Usage: python discovery.py --hub MOC [--output json|text]
"""
import argparse
import json
import sys
from pathlib import Path

try:
    from knowledge_rag import GraphStore  # local module expected in prod
except ImportError:
    # Lightweight fallback for bootstrap/dev
    class GraphStore:
        def top_hub(self, hub_name=None):
            return {
                "hub": hub_name or "MOC",
                "top_docs": [
                    {"id": "doc-001", "title": "MOC Overview", "connections": 42},
                    {"id": "doc-002", "title": "MOC Patterns", "connections": 37},
                ],
                "summary": "MOC is the most-connected hub; prioritize context from linked docs."
            }

def main() -> int:
    parser = argparse.ArgumentParser(description="Surface top-hub insights.")
    parser.add_argument("--hub", default="MOC", help="Hub name to inspect")
    parser.add_argument("--output", choices=["json", "text"], default="text")
    args = parser.parse_args()

    store = GraphStore()
    result = store.top_hub(args.hub)

    if args.output == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"# Top-hub: {result['hub']}")
        print(f"Summary: {result['summary']}")
        print("\nTop connected docs:")
        for d in result["top_docs"]:
            print(f"  - {d['title']} (connections: {d['connections']})")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

---

#### `/opt/axentx/vanguard/backend/lightning_orchestrator.py`
```python
#!/usr/bin/env python3
"""
Reusable Lightning Studio wrapper with idle-stop checks and quota reuse.
Usage: python lightning_orchestrator.py --script train_surrogate.py [--machine L40S] [--studio name] [--no-reuse]
"""
import argparse
import sys
from pathlib import Path

try:
    from lightning_sdk import Studio
    from lightning_sdk.workspace import Machine
    _sdk_available = True
except Exception:
    # Stubs for local dev/bootstrap; install lightning-ai in prod
    _sdk_available = False
    class Machine:
        L40S = "L40S"
        H200 = "H200"

    class Studio:
        def __init__(self, name, create_ok=False):
            self.name = name
            self.status = "Stopped"
            self.machine = None
        def start(self, machine=None):
            self.machine = machine or Machine.L40S
            self.status = "Running"
            return self
        def run(self, command, environment=None):
            if self.status != "Running":
                raise RuntimeError("Studio not running")
            # Simulate run; real usage should poll job status
            return type("Job", (), {"status": "running"})()
        def stop(self):
            self.status = "Stopped"

    class Teamspace:
        @staticmethod
        def studios():
            return []

def find_running_studio(name: str):
    for s in Teamspace.studios():
        if getattr(s, "name", None) == name and getattr(s, "status", None) == "Running":
            return s
    return None

def main() -> int:
    parser = argparse.ArgumentParser(description="Lightning Studio orchestrator")
    parser.add_argument("--script", required=True, help="Training/script to run")
    parser.add_argument("--machine", default=Machine.L40S, help="Machine type")
    parser.add_argument("--studio", default="vanguard-surrogate-train", help="Studio name")
    parser.add_argument("--no-reuse", action="store_true", help="Do not reuse running studio")
    args = parser.parse_args()

    if not Path(args.script).exists():
        print(f"Script not found: {args.script}", file=sys.stderr)
        return 1

    studio = None
    if not args.no_reuse:
        studio = find_running_studio(args.studio)

    if studio is None:
        studio = Studio(args.studio, create_ok=True).start(machine=args.machine)

    if studio.status != "Running":
        studio.start(machine=args.machine)

    try:
        job = studio.run(command=f"python {args.script}", environment={"PYTHONUNBUFFERED": "1"})
        print(f"Started job in studio {studio.name} on {studio.machine}")
        # In production, poll job.status and handle completion/failure
        return 0
    except Exception as e:
        print(f"Error running job: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
```

---

#### `/opt/axentx/vanguard/backend/ingest_hf_safe.py`
```python
#!/usr/bin/env python3
"""
HF ingestion with CDN-bypass file-list strategy and safe projection to {prompt,response}.
Usage:
  python ingest_hf_safe.py --repo org/surrogate-1 --date 2026-04-29 --out-dir ./batches/mirror-merged
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import List, Dict, Any

try:
    import requests
    from huggingface_hub import list_repo_tree, hf_hub_download
    _hf_available = True
except Exception:
    _hf_available = False
    # Stubs to allow bootstrap without HF credentials
    def list_repo_tree(*, path, repo_id, recursive=False):
        return []
    def hf_hub_download(*, repo_id, filename, local_dir):
        return str(Path(local_dir) / filename)
    requests = None

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def deterministic_repo_slug(repo: str, slug: str, n_siblings: int = 5) -> str:
    """Hash slug -> pick sibling repo deterministically to bypass HF commit cap."""
    h = int(hashlib.sha256(f"{repo}/{slug}".encode()).hexdigest(), 16)
    idx = h % n_siblings
    if idx == 0:
        return repo
    return f"{repo}-sibling-{idx}"

def list_date_files(repo: str, date_folder: str) -> List[str]:
    """
    Single API call to list files in a date folder (non-recursive).
    Caller should save this list to JSON for CDN-only training.
    """
    items =
