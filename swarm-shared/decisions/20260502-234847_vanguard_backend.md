# vanguard / backend

## Final Synthesized Implementation

**Core insight**: Persist once, train from CDN, shard writes deterministically, and keep studios alive.  
All contradictions are resolved in favor of correctness + concrete actionability.

### Directory setup
```bash
mkdir -p /opt/axentx/vanguard/{backend,manifests,scripts}
chmod +x /opt/axentx/vanguard/backend/*.py /opt/axentx/vanguard/scripts/*.sh
```

---

### 1) Deterministic HF sibling-repo selector  
`/opt/axentx/vanguard/backend/hf_repo_selector.py`
```python
import hashlib
from typing import List

SIBLING_REPOS: List[str] = [
    "axentx/surrogate-mirror-0",
    "axentx/surrogate-mirror-1",
    "axentx/surrogate-mirror-2",
    "axentx/surrogate-mirror-3",
    "axentx/surrogate-mirror-4",
]

def select_repo(slug: str) -> str:
    """Deterministic, stable shard assignment for HF commit-cap avoidance."""
    digest = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(digest, 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]
```

---

### 2) Cron-safe wrapper (executable)  
`/opt/axentx/vanguard/scripts/cron-safe-wrapper.sh`
```bash
#!/usr/bin/env bash
# Cron-safe wrapper: strict mode, logging, single instance via flock, retries.
# Usage (cron):
#   */30 * * * * /opt/axentx/vanguard/scripts/cron-safe-wrapper.sh /opt/axentx/vanguard/backend/orchestrate.py 2026-04-29

set -euo pipefail

SCRIPT_PATH="${1:-}"
DATE_FOLDER="${2:-}"
LOCKFILE="/var/lock/axentx_orchestrator.lock"
LOGDIR="/var/log/axentx"
LOGFILE="${LOGDIR}/orchestrator-$(date +%Y%m%d).log"
MAX_RETRIES=3
RETRY_DELAY=30

if [[ -z "$SCRIPT_PATH" || -z "$DATE_FOLDER" ]]; then
  echo "Usage: $0 <script.py> <date_folder>" >&2
  exit 1
fi

mkdir -p "$LOGDIR"
exec >>"$LOGFILE" 2>&1
echo "=== $(date -Iseconds) START $SCRIPT_PATH $DATE_FOLDER ==="

(
  flock -n 9 || { echo "Another instance is running; exiting."; exit 0; }

  attempt=0
  until python3 "$SCRIPT_PATH" "$DATE_FOLDER"; do
    attempt=$((attempt + 1))
    if (( attempt >= MAX_RETRIES )); then
      echo "FAILED after $MAX_RETRIES attempts."
      exit 1
    fi
    echo "Retry $attempt/$MAX_RETRIES in ${RETRY_DELAY}s..."
    sleep "$RETRY_DELAY"
  done

  echo "SUCCESS."
) 9>"$LOCKFILE"

echo "=== $(date -Iseconds) END ==="
```

---

### 3) Orchestrator (manifest + CDN script + studio guard)  
`/opt/axentx/vanguard/backend/orchestrate.py`
```python
#!/usr/bin/env python3
"""
Orchestrator:
- Persist HF file manifest (one date-scoped API call) to avoid 429.
- Generate CDN-only training script (no HF API/auth during training).
- Deterministic sibling-repo writes to respect 128/hr/repo commit cap.
- Lightning Studio lifecycle guard: reuse or restart; prevent idle-stop kills.
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import list_repo_tree, HfApi
from lightning import Lightning, Teamspace, Studio, Machine

from hf_repo_selector import select_repo

HF_REPO = "axentx/dataset-mirror"
MANIFEST_DIR = Path(__file__).parent.parent / "manifests"
TEMPLATE_PATH = Path(__file__).parent / "train_template.py"
OUTPUT_SCRIPT = Path(__file__).parent / "train.py"
LIGHTNING_TEAMSPACE = "axentx"
STUDIO_NAME = "vanguard-surrogate-train"

MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

def persist_manifest(date_folder: str) -> Path:
    """Single list_repo_tree call for the date folder; save JSON manifest."""
    entries = list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=False)
    files = sorted(e.path for e in entries if e.type == "file")
    manifest_path = MANIFEST_DIR / f"{date_folder.replace('/', '_')}.json"
    manifest_path.write_text(json.dumps({"date_folder": date_folder, "files": files}, indent=2))
    print(f"Manifest saved: {manifest_path} ({len(files)} files)")
    return manifest_path

def build_cdn_urls(date_folder: str, files: list) -> list:
    """Convert HF paths to public CDN URLs (no auth/rate-limit during training)."""
    base = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"
    return [f"{base}/{f}" for f in files if f.startswith(date_folder)]

def generate_training_script(cdn_urls: list, date_folder: str):
    """Render train_template.py with CDN file list."""
    template = TEMPLATE_PATH.read_text()
    rendered = template.replace("{{CDN_URLS}}", json.dumps(cdn_urls, indent=2))
    rendered = rendered.replace("{{DATE_FOLDER}}", date_folder)
    OUTPUT_SCRIPT.write_text(rendered)
    print(f"Training script written: {OUTPUT_SCRIPT}")

def get_or_start_studio(machine: str = "lightning-ai/L40S-1x") -> Studio:
    """Reuse running studio or start a new one (saves quota, prevents idle-stop kills)."""
    team = Teamspace(name=LIGHTNING_TEAMSPACE)
    for s in team.studios:
        if s.name == STUDIO_NAME and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s
    print(f"Starting studio {STUDIO_NAME} on {machine}")
    return Studio(
        name=STUDIO_NAME,
        teamspace=LIGHTNING_TEAMSPACE,
        machine=Machine(machine),
        create_ok=True,
    )

def run_training_in_studio(studio: Studio, script_path: Path):
    """Run training script in studio; restart if idle-stopped."""
    if studio.status != "Running":
        print(f"Studio stopped; restarting on L40S")
        studio.start(machine=Machine("lightning-ai/L40S-1x"))
    local_path = str(script_path)
    remote_path = f"/{script_path.name}"
    studio.upload(local_path, remote_path)
    result = studio.run(f"python {remote_path}", wait=True)
    print("Training run result:", result)
    return result

def commit_to_shard(file_path: str, content: bytes):
    """Write to a deterministic sibling repo to respect HF 128/hr/repo cap."""
    repo = select_repo(file_path)
    api = HfApi()
    api.upload_file(
        path_or_fileobj=content,
        path_in_repo=file_path,
        repo_id=repo,
        repo_type="dataset",
    )
    print(f"Committed {file_path} to {repo}")

def main(date_folder: str, use_studio: bool = False):
    manifest_path = persist_manifest(date_folder)
    manifest = json.loads(manifest_path.read_text())
    cdn_urls = build_cdn_urls(date_folder, manifest["files"])
    generate_training_script(cdn_urls, date_folder)

    if use_studio:
        studio = get_or_start_studio()
        run_training_in_studio(studio, OUTPUT_SCRIPT)

if __name__ == "__main__":
    # Usage: orchestrate.py <date_folder> [--studio]
    if len(sys.argv) < 2:
        print("Usage: orchestrate.py <date_folder> [--studio]")
        sys.exit(1)
    main(sys.argv[1], use_studio=("--studio" in sys.argv))
```

---

### 4) CDN-only training template  
`/opt/axent
