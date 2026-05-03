# airship / discovery

## Final Synthesized Implementation (Correct + Actionable)

I merged both proposals, kept the best parts, and resolved contradictions in favor of correctness and concrete actionability.

Key decisions:
- Use a **single, deterministic manifest generator** (Candidate 1’s structure + Candidate 2’s clarity).
- **Lightning Studio guard** uses the Lightning SDK correctly (Candidate 1’s approach is more complete; Candidate 2 omitted usable code).
- Training entrypoint supports `--manifest` and `--use-cdn-manifest` with **safe local fallback** (Candidate 2 requirement) while preserving Candidate 1’s fail-fast default behavior via env/CLI.
- All code is runnable with minimal dependencies and clear failure modes.

---

### 1) CDN manifest generator (run on Mac orchestration host)

`services/cdn/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate a CDN-only file manifest for Surrogate training.
Run after HF API window clears (or when repo tree is stable).
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("ERROR: huggingface_hub required. Install with: pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-training-data")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUTPUT_PATH = Path(os.getenv("MANIFEST_OUT", "training_file_manifest.json"))

def build_manifest() -> None:
    entries = list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_FOLDER,
        repo_type="dataset",
        recursive=False,
    )
    files = sorted(e.path for e in entries if e.type == "file")
    manifest = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "cdn_base": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main",
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {OUTPUT_PATH} ({len(files)} files)")

if __name__ == "__main__":
    try:
        build_manifest()
    except Exception as e:
        print(f"Failed to build manifest: {e}", file=sys.stderr)
        sys.exit(1)
```

Usage:
```bash
HF_DATASET_REPO=axentx/surrogate-training-data \
HF_DATE_FOLDER=2025-11-20 \
MANIFEST_OUT=./training_file_manifest.json \
python services/cdn/build_manifest.py
```

---

### 2) Lightning Studio lifecycle guard

`services/lightning/studio_guard.py`
```python
#!/usr/bin/env python3
"""
Ensure a Lightning Studio is running. Reuse if running; restart if stopped.
Designed for use before Lightning run() or remote launch.
"""
import os
import sys
import time

# Prefer SDK import; fail clearly if unavailable
try:
    from lightning.app import Lightning
    from lightning.app.utilities.cloud import _get_project
except ImportError:
    print("ERROR: lightning required. Install with: pip install lightning", file=sys.stderr)
    sys.exit(1)

LIGHTNING_PROJECT = os.getenv("LIGHTNING_PROJECT", "surrogate-training")
STUDIO_NAME = os.getenv("STUDIO_NAME", "surrogate-l40s-trainer")
MACHINE = os.getenv("LIGHTNING_MACHINE", "L40S")  # L40S available on free/prod tiers
MAX_RESTART_WAIT = int(os.getenv("MAX_RESTART_WAIT", "300"))

def ensure_studio_running() -> str:
    lightning = Lightning()
    teamspace = lightning.active_teamspace
    project = _get_project(teamspace, LIGHTNING_PROJECT)

    # Reuse if already running
    for studio in teamspace.studios:
        if studio.name == STUDIO_NAME and studio.status == "Running":
            print(f"Reusing running studio: {STUDIO_NAME} (id={studio.id})")
            return studio.id

    # Find existing (stopped/failed) or create new
    target = next((s for s in teamspace.studios if s.name == STUDIO_NAME), None)

    if target is None:
        print(f"Creating studio: {STUDIO_NAME}")
        target = lightning.create_studio(
            name=STUDIO_NAME,
            project=project,
            machine=MACHINE,
            create_ok=True,
        )
    else:
        print(f"Restarting studio: {STUDIO_NAME} (current={target.status})")
        target.start(machine=MACHINE)

    # Wait for running
    deadline = time.time() + MAX_RESTART_WAIT
    while time.time() < deadline:
        # Refresh listing
        for studio in teamspace.studios:
            if studio.name == STUDIO_NAME:
                if studio.status == "Running":
                    print(f"Studio running: {STUDIO_NAME} (id={studio.id})")
                    return studio.id
                if studio.status in ("Failed", "Error"):
                    raise RuntimeError(f"Studio {STUDIO_NAME} in bad state: {studio.status}")
        time.sleep(10)

    raise TimeoutError(f"Studio {STUDIO_NAME} did not start within {MAX_RESTART_WAIT}s")

if __name__ == "__main__":
    try:
        studio_id = ensure_studio_running()
        print(studio_id)
    except Exception as e:
        print(f"Studio guard failed: {e}", file=sys.stderr)
        sys.exit(1)
```

Usage:
```bash
LIGHTNING_PROJECT=surrogate-training \
STUDIO_NAME=surrogate-l40s-trainer \
python services/lightning/studio_guard.py
```

---

### 3) Training entrypoint with CDN support and safe fallback

`surrogate/training/train.py`
```python
#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

import requests
import torch
from torch.utils.data import IterableDataset, DataLoader

CDN_MANIFEST_DEFAULT = Path("training_file_manifest.json")

class CDNTextDataset(IterableDataset):
    """
    Stream files directly from CDN (no HF API calls during training).
    Expects manifest with: {"cdn_base": "...", "files": [...]}
    """
    def __init__(self, manifest_path: Path, max_files: int = None, timeout: int = 30):
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"CDN manifest missing: {manifest_path}. "
                "Run build_manifest.py or provide a valid manifest."
            )
        self.manifest = json.loads(manifest_path.read_text())
        self.files = self.manifest.get("files", [])
        if not self.files:
            raise ValueError("Manifest contains no files.")
        if max_files:
            self.files = self.files[:max_files]
        self.cdn_base = self.manifest.get("cdn_base")
        if not self.cdn_base:
            raise ValueError("Manifest missing cdn_base.")
        self.timeout = timeout

    def _stream_files(self):
        for rel in self.files:
            url = f"{self.cdn_base}/{rel}"
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            # Replace with your parser (parquet/jsonl -> {prompt, response})
            yield {"raw": resp.content, "path": rel}

    def __iter__(self):
        return self._stream_files()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--use-cdn-manifest", action="store_true", default=True,
                        help="Use CDN manifest for data loading (default: True).")
    parser.add_argument("--manifest", type=str, default=str(CDN_MANIFEST_DEFAULT),
                        help="Path to CDN manifest JSON.")
    parser.add_argument("--local-fallback", action="store_true", default=False,
                        help="If manifest missing and this is set, use local files instead of failing.")
    args = parser.parse_args()

    dataset = None
    if args.use_cdn_manifest:
        manifest_path = Path(args.manifest)
        try:
            dataset = CDNTextDataset(manifest_path)
            print
