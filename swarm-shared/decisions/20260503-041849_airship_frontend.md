# airship / frontend

## Highest-Value Incremental Improvement (ship <2h)

**What:** Add a CDN-only training manifest + Lightning Studio auto-restart guard to Surrogate AI so training jobs never hit HF API rate limits and survive idle timeouts.

**Why:**  
- Eliminates 429s by replacing `list_repo_files`/`load_dataset` API calls during training with a pre-computed CDN file list.  
- Survives Lightning idle-stop (training dies when studio stops) by auto-restarting the target machine before `.run()`.  
- Fits existing patterns: `list_repo_tree` once → JSON manifest → embed in train script; reuse/start studios deterministically.

**Scope:**  
- Single PR touching only Surrogate training orchestration (no model code, no infra changes).  
- Works with existing `docker-compose.microservices.yml` and `surrogate/` layout.

---

## Implementation Plan

### 1) Create manifest generator (run once on Mac/orchestrator)
- Path: `surrogate/scripts/build_cdn_manifest.py`
- Uses HF API **once** (respect rate limits) to `list_repo_tree` for a date folder (e.g., `batches/mirror-merged/2026-05-03/`).
- Emits `surrogate/configs/cdn_manifest.json` with public CDN URLs and local basenames.
- Commit manifest alongside training code (or optionally .gitignore and produce at runtime before training).

### 2) Update training data loader to use CDN-only fetches
- Path: `surrogate/train/data.py` (or wherever dataset loading lives).
- Replace `load_dataset(streaming=True)` with a lightweight `IterableDataset` that streams from `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth, no API calls).
- Parse only `{prompt, response}` at read time; ignore extra columns/schema mismatches.

### 3) Add Lightning Studio lifecycle guard
- Path: `surrogate/scripts/launch_training.py`
- Before `Studio(...).run(...)`, list running studios; reuse if same name+running.
- If stopped, restart with `target.start(machine=Machine.L40S)` (or fallback to free-tier cloud order).
- Wrap `.run()` with status check and auto-restart on unexpected stop.

### 4) Wire it together
- Update training entrypoint to accept `--manifest` (default: `configs/cdn_manifest.json`).
- Ensure training script is executable and has `#!/usr/bin/env bash` wrappers if invoked via cron/launcher.

---

## Code Snippets

### surrogate/scripts/build_cdn_manifest.py
```python
#!/usr/bin/env python3
"""
Generate a CDN-only manifest for Surrogate training.
Run once (or per date folder) on orchestrator after HF API rate-limit window clears.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-mirror")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
BASE_PATH = f"batches/mirror-merged/{DATE_FOLDER}"
OUTPUT_PATH = Path(__file__).parent.parent / "configs" / "cdn_manifest.json"

def main() -> None:
    api = HfApi()
    try:
        items = api.list_repo_tree(
            repo_id=HF_REPO,
            path=BASE_PATH,
            recursive=False,  # one folder only; avoids heavy pagination
        )
    except Exception as exc:
        print(f"Failed to list {HF_REPO}/{BASE_PATH}: {exc}", file=sys.stderr)
        sys.exit(1)

    files = [p.rpath for p in items if p.rpath and not p.rpath.endswith("/")]
    manifest = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "base_path": BASE_PATH,
        "cdn_base": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main",
        "files": sorted(files),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_files": len(files),
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {OUTPUT_PATH}")

if __name__ == "__main__":
    main()
```

### surrogate/train/data.py (CDN-only iterable dataset)
```python
import json
import logging
from pathlib import Path
from typing import Dict, Iterator, Optional

import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

logger = logging.getLogger(__name__)

class CDNParquetIterableDataset(IterableDataset):
    """
    Stream parquet files from HF CDN (no auth, no API calls).
    Expects manifest produced by build_cdn_manifest.py.
    Projects only {prompt, response} at parse time.
    """

    def __init__(self, manifest_path: str, repo: str, start_idx: int = 0, max_files: Optional[int] = None):
        manifest_path = Path(manifest_path)
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.repo = repo
        self.cdn_base = manifest.get("cdn_base") or f"https://huggingface.co/datasets/{repo}/resolve/main"
        files = manifest.get("files", [])
        if max_files:
            files = files[start_idx : start_idx + max_files]
        else:
            files = files[start_idx:]
        self.file_paths = [f"{self.cdn_base}/{p}" for p in files]

    def _stream_file(self, url: str) -> Iterator[Dict[str, str]]:
        # CDN download: no Authorization header -> bypasses API rate limits
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        with open("/tmp/temp.parquet", "wb") as f:
            f.write(resp.content)

        table = pq.read_table("/tmp/temp.parquet", columns=["prompt", "response"])
        for row in table.to_pylist():
            # Keep only required fields; tolerate missing columns
            yield {
                "prompt": row.get("prompt") or "",
                "response": row.get("response") or "",
            }

    def __iter__(self) -> Iterator[Dict[str, str]]:
        for url in self.file_paths:
            try:
                yield from self._stream_file(url)
            except Exception as exc:
                logger.warning(f"Failed to stream {url}: {exc}")
                continue
```

### surrogate/scripts/launch_training.py (Lightning Studio guard)
```bash
#!/usr/bin/env bash
set -euo pipefail
# Wrapper to launch Surrogate training with Lightning Studio lifecycle resilience.
# Ensures studio is running and restarts machine on idle-stop before .run().

export SHELL=/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="${MANIFEST:-$REPO_ROOT/configs/cdn_manifest.json}"
STUDIO_NAME="${STUDIO_NAME:-surrogate-training}"
MACHINE="${MACHINE:-L40S}"

cd "$REPO_ROOT"

# Prefer Lightning reuse pattern: list and reuse running studio
python3 - <<PY
import os
from lightning_sdk import Studio, Machine, Teamspace

studio_name = os.getenv("STUDIO_NAME", "surrogate-training")
machine = os.getenv("MACHINE", "L40S")
manifest = os.getenv("MANIFEST", "")

teamspace = Teamspace()
running = None
for s in teamspace.studios:
    if s.name == studio_name and s.status == "running":
        running = s
        break

if running:
    print(f"Reusing running studio: {studio_name}")
    studio = running
else:
    print(f"Creating studio: {studio_name}")
    studio = Studio(
        name=studio_name,
        machine=Machine(machine),
        create_ok=True,
    )

# If studio is stopped, restart machine before run
if studio.status != "running":
    print(f"Studio stopped
