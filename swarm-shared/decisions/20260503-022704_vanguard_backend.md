# vanguard / backend

## Final Synthesized Implementation (Best of Both Candidates)

### Diagnosis (Consolidated)
- **Quota exhaustion**: Every training run performs authenticated `list_repo_tree`/`load_dataset` calls → HF API 429 risk.
- **CDN bypass missing**: Training streams via HF API instead of raw CDN URLs → misses high-limit tier.
- **Schema heterogeneity**: `load_dataset(streaming=True)` on mixed-file repos → pyarrow `CastError`.
- **Lightning Studio waste**: Script recreates studios instead of reusing running ones → ~80+ hrs/mo quota loss.
- **Idle-stop fragility**: No pre-run status check; idle timeout kills training without restart.

### Scope
- `/opt/axentx/vanguard/backend/data/manifest.py` (new)
- `/opt/axentx/vanguard/backend/training/train.py` (modified)
- Optional Mac orchestrator helper (non-breaking)

---

### 1. Create: `backend/data/manifest.py`
```python
import json
import os
from pathlib import Path
from typing import List, Optional

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/company/mirror-merged")
MANIFEST_DIR = Path(__file__).parent.parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

def list_date_folder(
    date_folder: str,
    recursive: bool = False,
    allowed_extensions: Optional[List[str]] = None
) -> List[str]:
    """
    Single API call to list files in a date folder.
    Returns relative paths under the date folder.
    """
    api = HfApi()
    tree = api.list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=recursive)
    files = [item.path for item in tree if item.type == "file"]

    if allowed_extensions:
        exts = tuple(allowed_extensions)
        files = [f for f in files if f.lower().endswith(exts)]

    return sorted(files)

def save_manifest(date_folder: str, files: List[str]) -> Path:
    out = MANIFEST_DIR / f"{date_folder.rstrip('/')}_files.json"
    out.write_text(json.dumps({"date_folder": date_folder, "files": files}, indent=2))
    return out

def load_manifest(date_folder: str) -> List[str]:
    out = MANIFEST_DIR / f"{date_folder.rstrip('/')}_files.json"
    if not out.exists():
        raise FileNotFoundError(f"Manifest missing: {out}")
    data = json.loads(out.read_text())
    return data["files"]
```

---

### 2. Update: `backend/training/train.py`
```python
import os
import json
import time
import requests
from pathlib import Path
from typing import Iterator, Dict, Any, List

import torch
from lightning import Fabric
from lightning.pytorch import Trainer
from lightning.pytorch.callbacks import ModelCheckpoint

from axentx.data.manifest import load_manifest, MANIFEST_DIR

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/company/mirror-merged")
CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"

# --- CDN streaming with schema normalization ---
def cdn_stream_files(
    date_folder: str,
    allowed_extensions: List[str] = (".json", ".jsonl", ".txt"),
    timeout: float = 30.0
) -> Iterator[Dict[str, str]]:
    """
    Yield {prompt, response} pairs by downloading files via CDN (no HF API auth).
    Projects heterogeneous files to {prompt, response} at parse time.
    """
    files = load_manifest(date_folder)

    for fpath in files:
        if allowed_extensions and not fpath.lower().endswith(allowed_extensions):
            continue

        url = f"{CDN_BASE}/{fpath}"
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
        except Exception as exc:
            print(f"Failed to fetch {url}: {exc}")
            continue

        text = resp.text.strip()
        if not text:
            continue

        rows: List[Dict[str, Any]] = []

        # Try JSON array first
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                rows.extend(parsed)
            elif isinstance(parsed, dict):
                rows.append(parsed)
            else:
                raise ValueError("Unexpected JSON root type")
        except Exception:
            # Fallback: line-by-line JSONL
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    rows.append({"text": line})

        for row in rows:
            if not isinstance(row, dict):
                continue

            prompt = row.get("prompt") or row.get("input") or row.get("text") or ""
            response = row.get("response") or row.get("output") or ""

            # If both missing, skip; if only one present, mirror to the other minimally
            if not prompt and not response:
                continue
            if not prompt:
                prompt = response
            if not response:
                response = prompt

            yield {"prompt": prompt, "response": response}

def collate_fn(batch: List[Dict[str, str]]) -> Dict[str, List[str]]:
    return {k: [item[k] for item in batch] for k in batch[0]}

# --- Studio reuse + idle-restart guard ---
def get_or_create_studio(name: str = "vanguard-surrogate"):
    try:
        from lightning.pytorch.cloud import Studio
        from lightning.pytorch.cloud.machine import Machine

        studios = Studio.list()
        studio = next((s for s in studios if s.name == name), None)

        if studio:
            if studio.status == "Running":
                print(f"Reusing running studio: {studio.name}")
                return studio
            else:
                print(f"Starting stopped studio: {studio.name}")
                studio.start(machine=Machine.L40S)
                return studio

        print(f"Creating new studio: {name}")
        return Studio.create(
            name=name,
            machine=Machine.L40S,
            code_dir=str(Path(__file__).parent.parent.parent),
        )
    except Exception as exc:
        print(f"Studio setup failed ({exc}), falling back to local execution")
        return None

# --- Training entrypoint ---
def train(date_folder: str, max_steps: int = 1000):
    fabric = Fabric()
    fabric.launch()

    from axentx.models.lightning_wrapper import SurrogateModule  # adjust import as needed

    studio = get_or_create_studio()

    model = SurrogateModule()

    # Build dataset once to avoid re-streaming on every epoch
    dataset = list(cdn_stream_files(date_folder))
    if not dataset:
        raise RuntimeError("No valid samples found in manifest; check date_folder and files.")

    train_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=8,
        collate_fn=collate_fn,
        num_workers=0,
        shuffle=True,
    )

    trainer = Trainer(
        max_steps=max_steps,
        accelerator="auto",
        devices="auto",
        precision="bf16-mixed",
        callbacks=[ModelCheckpoint(monitor="loss")],
    )

    if studio is not None:
        # Guard against idle-stop: ensure running before fit
        if hasattr(studio, "status") and studio.status != "Running":
            from lightning.pytorch.cloud.machine import Machine
            studio.start(machine=Machine.L40S)
        studio.run(trainer.fit, model, train_loader)
    else:
        trainer.fit(model, train_loader)

if __name__ == "__main__":
    import sys
    date_folder = sys.argv[1] if len(sys.argv) > 1 else "2026-04-29"
    train(date_folder)
```

---

### 3. Optional Mac Orchestrator Helper
`bin/gen_manifest.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail
cd /opt/axentx/vanguard
DATEFOLDER="${1:-2026-04-29}"
python -c "
from axentx.data.manifest import list_date_folder, save_manifest
import
