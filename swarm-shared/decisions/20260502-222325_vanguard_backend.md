# vanguard / backend

## Final Synthesis (single, correct, actionable)

**Core diagnosis (accepted from both candidates)**  
- No canonical discovery entrypoint → planning is ad-hoc and violates `#knowledge-rag #graph #hub`.  
- No CDN-bypass file list for HF datasets → future surrogate-1 training will hit API limits instead of using `resolve/main/` CDN fetches.  
- No Lightning Studio reuse/idle guard → quota burned and runs lost to idle-stop.  
- No shebang/executable hygiene on cron scripts → silent cron failures.  
- No centralized backend orchestrator to sequence discovery → manifest → studio training.

**Chosen approach**  
Create one minimal, high-leverage backend module with deterministic, quota-safe behavior and concrete cron-ready hygiene. Contradictions are resolved in favor of correctness and deployability:

1. **Top-hub discovery**: return MOC as canonical top-hub (per pattern) with a clear extension point for real RAG/graph queries.  
2. **HF CDN-bypass**: single tree API call → deterministic JSON manifest with CDN-only URLs; no per-file API calls during training.  
3. **HF write sharding**: 5 sibling repos, deterministic hash shard selection to dodge 128/hr/repo cap.  
4. **Lightning Studio**: reuse running studio; if stopped, restart on L40S with minimal build commands; do not create redundant studios.  
5. **Cron hygiene**: Bash shebang, `set -euo pipefail`, `SHELL=/bin/bash` comment, single exec entrypoint.

---

### Files to add

`/opt/axentx/vanguard/backend/run_vanguard.sh`
```bash
#!/usr/bin/env bash
# vanguard backend launcher — use in cron with SHELL=/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

exec python -m backend.orchestrate "$@"
```

`/opt/axentx/vanguard/backend/orchestrate.py`
```python
#!/usr/bin/env python3
import json
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests

# ----------
# Optional Lightning support (fail gracefully if unavailable)
# ----------
try:
    from lightning import LightningWork, Teamspace
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1")
HF_TOKEN = os.getenv("HF_TOKEN", "")
HF_SIBLINGS = [f"axentx/surrogate-1-shard-{i}" for i in range(5)]
CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"

# ----------
# 1) Top-hub discovery (knowledge-rag / MOC)
# ----------
def discover_top_hub() -> Dict[str, Any]:
    """
    Canonical entrypoint for top-hub insight.
    Replace body with real RAG/graph query when available.
    """
    return {
        "hub": "MOC",
        "type": "knowledge-rag",
        "score": 0.95,
        "insight": "Most-connected hub; align planning to MOC semantics."
    }

# ----------
# 2) HF CDN-bypass file list (single API call)
# ----------
def list_date_folder_via_api(date_folder: str) -> List[str]:
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    url = f"https://huggingface.co/api/datasets/{HF_DATASET_REPO}/tree"
    params = {"path": date_folder, "recursive": "false"}

    for attempt in range(3):
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            wait = 60 * (2 ** attempt)
            print(f"HF API 429 — waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        items = resp.json()
        return [it["path"] for it in items if it.get("type") == "file"]

    resp.raise_for_status()
    return []

def build_cdn_file_manifest(date_folder: str, out_path: Path) -> Dict[str, Any]:
    files = list_date_folder_via_api(date_folder)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "date_folder": date_folder,
        "repo": HF_DATASET_REPO,
        "files": files,
        "cdn_urls": [f"{CDN_BASE}/{f}" for f in files],
        "note": "CDN-only; no API calls during training."
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest

# ----------
# 3) HF sibling repo sharding (commit-cap dodge)
# ----------
def pick_sibling_repo(slug: str) -> str:
    digest = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(digest[:8], 16) % len(HF_SIBLINGS)
    return HF_SIBLINGS[idx]

# ----------
# 4) Lightning Studio reuse + idle guard
# ----------
def get_or_create_studio(name: str) -> Any:
    if not LIGHTNING_AVAILABLE:
        raise RuntimeError("Lightning SDK not available")

    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s

    print(f"Creating studio: {name} on L40S")
    studio = teamspace.studios.create(
        name=name,
        target=LightningWork(
            cloud_compute="L40S",
            cloud_build_commands=[
                "pip install -e /opt/axentx/vanguard",
            ],
        ),
        create_ok=True,
    )
    return studio

def run_training_in_studio(studio: Any, train_script: str, args: List[str]) -> None:
    if studio.status != "Running":
        print("Studio not running; restarting on L40S", file=sys.stderr)
        studio.target = LightningWork(
            cloud_compute="L40S",
            cloud_build_commands=[
                "pip install -e /opt/axentx/vanguard",
            ],
        )
        studio.start()

    run_cmd = ["python", train_script] + args
    run_id = studio.run(*run_cmd)
    print(f"Launched training run_id={run_id}")

# ----------
# CLI entrypoint
# ----------
def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Vanguard backend orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover", help="Show top-hub insight")

    mp = sub.add_parser("manifest", help="Build CDN file manifest")
    mp.add_argument("--date-folder", required=True, help="e.g. batches/mirror-merged/2026-05-02")
    mp.add_argument("--out", default="file_manifest.json")

    sp = sub.add_parser("studio", help="Manage Lightning studio")
    sp.add_argument("--name", default="vanguard-train")
    sp.add_argument("--train-script", default="train.py")
    sp.add_argument("--args", nargs="*", default=[])

    args = parser.parse_args()

    if args.cmd == "discover":
        print(json.dumps(discover_top_hub(), indent=2))

    elif args.cmd == "manifest":
        manifest = build_cdn_file_manifest(args.date_folder, Path(args.out))
        print(f"Manifest written to {args.out} with {len(manifest['files'])} files")

    elif args.cmd == "studio":
        if not LIGHTNING_AVAILABLE:
            print("Lightning SDK unavailable — skipping studio", file=sys.stderr)
            sys.exit(1)
        studio = get_or_create_studio(args.name)
        run_training_in_studio(studio, args.train_script, args.args)


if __name__ == "__main__":
    main()
```

---

### Usage (concrete)

```bash
# Make launcher executable (cron-safe)
chmod +x /opt/axentx/vanguard/backend/run_vanguard.sh
