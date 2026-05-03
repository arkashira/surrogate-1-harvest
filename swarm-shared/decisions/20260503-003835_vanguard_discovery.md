# vanguard / discovery

## Final Actionable Plan (Synthesized)

**Core problem**: repeated HF API enumeration and missing Lightning Studio reuse burn quota and risk 429s/PyArrow schema failures.  
**Goal**: one-time manifest generation → CDN-only training → deterministic reuse of running Lightning Studio with safe fallback.

---

### 1) Create manifest generator (single non-recursive API call)

```bash
mkdir -p /opt/axentx/vanguard/{scripts/discovery,manifests,configs}
```

```python
# /opt/axentx/vanguard/scripts/discovery/persist_manifest.py
#!/usr/bin/env python3
"""
Generate and persist a CDN-only file manifest for (repo, dateFolder).
Usage:
    python persist_manifest.py --repo huggingface/datasets/my_repo \
                               --date 2026-04-29 \
                               --out-dir /opt/axentx/vanguard/manifests
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import HfApi, HFError
except ImportError:
    print("ERROR: huggingface_hub not installed. Run: pip install huggingface_hub")
    sys.exit(1)

MAX_RETRIES = 3
BACKOFF = 30

def build_manifest(repo_id: str, date_folder: str, out_dir: Path) -> Path:
    api = HfApi()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Single non-recursive call per dateFolder (avoids pagination explosion)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            items = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
            break
        except HFError as exc:
            if attempt == MAX_RETRIES:
                print(f"HF API error after {MAX_RETRIES} attempts: {exc}")
                raise
            wait = BACKOFF * attempt
            print(f"Attempt {attempt}/{MAX_RETRIES} failed ({exc}). Retrying in {wait}s...")
            time.sleep(wait)

    # Keep only files and build CDN URLs
    files = []
    date_prefix = f"{date_folder}/"
    for item in items:
        p = Path(item.path)
        if getattr(item, "type", None) != "file" and "." not in p.name:
            continue
        cdn_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{item.path}"
        files.append({
            "repo": repo_id,
            "path": str(item.path),
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None),
            "lfs": bool(getattr(item, "lfs", False))
        })

    manifest = {
        "repo": repo_id,
        "date_folder": date_folder,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "strategy": "cdn-only",
        "note": "Use CDN URLs during training to bypass HF API rate limits.",
        "files": files
    }

    slug = repo_id.replace("/", "_")
    out_path = out_dir / f"{slug}__{date_folder}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path} ({len(files)} files)")
    return out_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Persist CDN manifest for repo/dateFolder")
    parser.add_argument("--repo", required=True, help="HF repo id (e.g., org/dataset)")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-04-29)")
    parser.add_argument("--out-dir", default="/opt/axentx/vanguard/manifests", help="Output directory")
    args = parser.parse_args()

    build_manifest(args.repo, args.date, Path(args.out_dir))
```

---

### 2) Orchestration wrapper (reuse + safe fallback)

```bash
# /opt/axentx/vanguard/scripts/run_vanguard_training.sh
#!/usr/bin/env bash
#
# Orchestration wrapper for vanguard surrogate-1 training.
# Enforces: Mac runs orchestration only; training runs on Lightning.
#
# Usage:
#   bash run_vanguard_training.sh --repo org/dataset --date 2026-04-29

set -euo pipefail
export SHELL=/bin/bash

REPO=""
DATEFOLDER=""
MANIFEST_DIR="/opt/axentx/vanguard/manifests"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

while [[ $# -gt 0 ]]; do
  case $1 in
    --repo) REPO="$2"; shift ;;
    --date) DATEFOLDER="$2"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

if [[ -z "$REPO" || -z "$DATEFOLDER" ]]; then
  echo "Usage: $0 --repo org/repo --date YYYY-MM-DD"
  exit 1
fi

SLUG=$(echo "$REPO" | tr '/' '_')
MANIFEST="${MANIFEST_DIR}/${SLUG}__${DATEFOLDER}.json"

# 1) Generate or reuse manifest
if [[ ! -f "$MANIFEST" ]]; then
  echo "Manifest missing. Generating..."
  python3 "${SCRIPT_DIR}/discovery/persist_manifest.py" --repo "$REPO" --date "$DATEFOLDER" --out-dir "$MANIFEST_DIR"
else
  echo "Using existing manifest: $MANIFEST"
fi

# 2) Reuse running Lightning Studio if available; safe fallback if stopped
python3 - <<PY
import os, sys, time
from pathlib import Path

try:
    from lightning_sdk import Studio, Teamspace
except ImportError:
    print("ERROR: lightning_sdk not installed. Run: pip install lightning")
    sys.exit(1)

MANIFEST = os.getenv("MANIFEST")
REPO = os.getenv("REPO")
DATEFOLDER = os.getenv("DATEFOLDER")

studio_name = f"vanguard-{REPO.replace('/', '-')}-{DATEFOLDER}"
team = Teamspace()

studio = None
reuse = False
for s in team.studios:
    if s.name == studio_name:
        studio = s
        if s.status == "running":
            print(f"Reusing running studio: {s.name}")
            reuse = True
        else:
            print(f"Found stopped studio: {s.name}. Restarting...")
            try:
                s.start()
                reuse = True
            except Exception as exc:
                print(f"Failed to restart studio: {exc}")
        break

if not reuse:
    print(f"Creating studio: {studio_name}")
    # Free tier falls to L40S max; H200 requires lightning-lambda-prod
    studio = Studio(
        name=studio_name,
        machine="L40S",
        create_ok=True
    )

# Upload manifest and run training (non-blocking)
manifest_path = Path(MANIFEST)
studio.upload(str(manifest_path), "manifests/")
studio.run("pip install -r requirements.txt", wait=True)
# Start long-running training (detached)
studio.run(f"python train.py --manifest manifests/{manifest_path.name}", detach=True)
PY

echo "Training orchestration submitted for ${REPO} @ ${DATEFOLDER}"
```

```bash
chmod +x /opt/axentx/vanguard/scripts/discovery/persist_manifest.py
chmod +x /opt/axentx/vanguard/scripts/run_vanguard_training.sh
```

---

### 3) Training script expectations (minimal change)

Your `train.py` should accept `--manifest` and read the JSON, then stream files via CDN URLs (e.g., with `wget`/`curl` or `datasets` loading from local files). Example snippet:

```python
import json, argparse
from datasets import load_dataset

parser = argparse.ArgumentParser()
parser.add_argument("--manifest", required=True)
args = parser.parse_args()

with open(args.manifest) as f:
    manifest = json.load(f)

# Option A: use CDN URLs
