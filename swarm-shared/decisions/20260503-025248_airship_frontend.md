# airship / frontend

## Final Synthesized Implementation

**Core Strategy**: Pre-generate a CDN-only manifest on the Mac (single API call outside rate-limit windows), then run Lightning Studio training with zero HF API calls during data loading. Reuse running studios to save quota and auto-restart on idle-stop kills.

**Key Decisions**:
- Use **CDN direct downloads** (not `hf_hub_download`) during training to eliminate 429s entirely.
- **Reuse running studios** instead of creating new ones (saves ~80hr/mo quota).
- **Auto-restart** on idle-stop with exponential backoff.
- **Column projection at parse time** to handle mixed schemas without schema validation failures.

---

## 1. CDN Manifest Generator (`/opt/axentx/airship/surrogate/training/gen_manifest.py`)

```python
#!/usr/bin/env python3
"""
Generate CDN-only manifest for Surrogate training.
Run from Mac after HF API rate-limit window clears.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Installing huggingface_hub...")
    os.system("pip install huggingface_hub -q")
    from huggingface_hub import HfApi

HF_REPO = "axentx/surrogate-training-data"
DATE_FOLDER = datetime.now(timezone.utc).strftime("%Y-%m-%d")
OUTPUT_DIR = Path(__file__).parent / "manifests"
OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

def gen_manifest(date_override: str = None) -> Path:
    """Generate manifest with CDN URLs only (zero API calls during training)."""
    target_date = date_override or DATE_FOLDER
    api = HfApi()
    
    # Single API call - recursive=False avoids pagination explosion
    tree = api.list_repo_tree(
        repo_id=HF_REPO,
        path=target_date,
        recursive=False,
        repo_type="dataset",
    )
    
    files = [
        item.rfilename for item in tree
        if item.rfilename.endswith((".parquet", ".jsonl"))
    ]
    
    if not files:
        print(f"⚠️  No parquet/jsonl files found for {target_date}")
        sys.exit(1)
    
    # CDN URLs bypass HF API auth/rate limits entirely
    manifest = {
        "date": target_date,
        "repo": HF_REPO,
        "files": [
            {
                "path": f,
                "cdn_url": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{target_date}/{f}",
                "hf_hub_download": f"{target_date}/{f}",
            }
            for f in sorted(files)
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_files": len(files),
    }
    
    out_path = OUTPUT_DIR / f"manifest-{target_date}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"✓ Manifest written to {out_path}")
    print(f"  {len(files)} files, CDN-only (zero API calls during training)")
    return out_path

if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    gen_manifest(date_override=date_arg)
```

---

## 2. Lightning Studio Reuse + Idle-Stop Guard (`/opt/axentx/airship/surrogate/training/run_lightning.py`)

```bash
#!/usr/bin/env bash
# Surrogate training launcher with Lightning Studio reuse + idle-stop guard
# Prevents quota waste from recreating studios; auto-restarts on idle-timeout
set -euo pipefail

export SHELL=/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_DATE="${1:-$(date +%Y-%m-%d)}"
MANIFEST_FILE="${SCRIPT_DIR}/manifests/manifest-${MANIFEST_DATE}.json"
MAX_RETRIES="${MAX_RETRIES:-3}"
RETRY_DELAY="${RETRY_DELAY:-30}"

if [[ ! -f "$MANIFEST_FILE" ]]; then
    echo "❌ Manifest not found: $MANIFEST_FILE"
    echo "   Run: python gen_manifest.py [YYYY-MM-DD]"
    exit 1
fi

python3 - "$MANIFEST_FILE" "$MAX_RETRIES" "$RETRY_DELAY" <<'PYEOF'
import sys
import time
from pathlib import Path

import lightning as L

MANIFEST_FILE = Path(sys.argv[1])
MAX_RETRIES = int(sys.argv[2])
RETRY_DELAY = int(sys.argv[3])
SCRIPT_DIR = Path(__file__).parent

def reuse_or_create_studio():
    """Reuse running studio or create new one (saves 80hr/mo quota)."""
    teamspace = L.Teamspace()
    studio_name = "surrogate-training"
    
    # Reuse if already running
    for studio in teamspace.studios:
        if studio.name == studio_name and studio.status == "running":
            print(f"♻️  Reusing running studio: {studio_name}")
            return studio
    
    # Create new if not running
    print(f"🆕 Creating studio: {studio_name}")
    return L.Studio(
        name=studio_name,
        create_ok=True,
        machine="L40S",  # Free tier falls to L40S; H200 requires lightning-lambda-prod
    )

def ensure_running_with_retry(studio, max_retries=MAX_RETRIES, retry_delay=RETRY_DELAY):
    """Restart studio if idle-stop killed it, with exponential backoff."""
    for attempt in range(max_retries + 1):
        if studio.status == "running":
            return studio
        
        if attempt == max_retries:
            raise RuntimeError(f"Studio failed to start after {max_retries} retries")
        
        wait_time = retry_delay * (2 ** attempt)
        print(f"⚠️  Studio stopped (idle-timeout), restarting in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
        time.sleep(wait_time)
        studio.start(machine="L40S")
    
    return studio

def main():
    studio = reuse_or_create_studio()
    studio = ensure_running_with_retry(studio)
    
    # Run training with CDN-only manifest (zero HF API calls)
    job = studio.run(
        str(SCRIPT_DIR / "train_cdn.py"),
        arguments=[str(MANIFEST_FILE)],
        wait=False,  # Non-blocking
    )
    print(f"🚀 Training started: {job.name}")
    print(f"   Manifest: {MANIFEST_FILE.name}")
    print(f"   Monitor: lightning.ai/teamspace/studios/{studio.name}")

if __name__ == "__main__":
    main()
PYEOF
```

---

## 3. CDN-Only Training Loader (`/opt/axentx/airship/surrogate/training/train_cdn.py`)

```python
#!/usr/bin/env python3
"""
CDN-only training loader - zero HF API calls during data load.
Uses pre-cached manifest to bypass rate limits.
"""
import json
import pyarrow.parquet as pq
import pyarrow as pa
from pathlib import Path
from typing import Dict, List, Optional
import requests
from datasets import Dataset, Features, Value
import tempfile
import sys

def load_from_cdn(manifest_path: Path, max_files: Optional[int] = None) -> Dataset:
    """
    Load dataset via CDN URLs (no HF API auth/rate limits).
    Projects to {prompt, response} only at parse time.
    """
    manifest = json.loads(manifest_path.read_text())
    files = manifest["files"]
    
    if max_files:
        files = files[:max_files]
    
    all_rows: List[Dict[str, str]] = []
    
    for idx, file_info in enumerate(files):
        cdn_url = file_info["cdn_url"]
        print(f"[{idx+1}/{len(files)}] Downloading (CDN): {file_info['path']}")
        
        try:
            # CDN download - no Authorization header, bypasses /api/ rate limits
            resp = requests.get(cdn_url, timeout=60)
            resp.raise_for_status()
            
            # Write temp file for pyarrow
            with tempfile.NamedTemporaryFile(suffix=".par
