# airship / frontend

**Final Synthesized Implementation**  
*(Best parts merged; contradictions resolved for correctness + concrete actionability; ships <2h)*

---

## 1) Core improvement (highest-value)
**Resilient Surrogate-1 training pipeline**  
- Replace repeated HF API calls during training with a **single-shot `training-manifest.json`** containing **CDN-resolved URLs**.  
- Eliminates 429 rate limits and Lightning idle-stop quota waste.  
- Manifest is generated once (ideally on Mac orchestration host), committed, and reused by training jobs with **zero HF API calls during training**.

---

## 2) Implementation plan (corrected + actionable)

### Step 1 — Generate manifest (non-recursive, CDN URLs)
- Use `huggingface_hub.HfApi.list_repo_tree(recursive=False)` on the **date folder** (e.g. `2024-06-12`) to avoid pagination/rate limits.  
- Emit `data/training-manifest.json` with:
  - `cdn_url` (public dataset file URL)  
  - `local_path` (optional, for pre-cached files)  
  - metadata (`generated_at`, `repo_id`, `date_folder`, `file_count`)  
- Commit this file to the repo so Lightning jobs consume it without any HF auth/API.

### Step 2 — Update training script to use manifest
- Load `training-manifest.json`.  
- Prefer **local cached files** when available (fast, no network); fall back to CDN URLs only if cache missing.  
- Project each record to `{prompt, response}` (strict schema).  
- Use `datasets.load_dataset(..., streaming=False)` with `data_files=[...]` (small manifest, safe).  
- Keep `streaming=False` for deterministic epochs; if memory-constrained, use `IterableDataset`/batches instead of changing manifest semantics.

### Step 3 — Robust launcher + studio reuse + auto-restart
- Add executable launcher script (`scripts/run_training.sh`) that:
  - Checks studio status; if stopped, waits/restarts as needed.  
  - Runs manifest generation only if missing or stale (configurable).  
  - Executes training with proper env and logging.  
- Ensure `SHELL=/bin/bash` compatibility and idempotency.

### Step 4 — Integrate into orchestration
- Add manifest generation as a pre-train step in `docker-compose.yml` or CI/CD (optional but recommended).  
- Ensure HF token only required for manifest generation (not training).

---

## 3) Corrected, production-ready code

### `scripts/generate-training-manifest.py`
```python
#!/usr/bin/env python3
"""
Generate training-manifest.json for Surrogate-1 training.
Run on orchestration host (Mac/CI) after HF rate-limit window clears.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-1-ingest"
DATE_FOLDER = os.getenv("SURROGATE_DATE_FOLDER") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
OUTPUT_PATH = Path(__file__).parent.parent / "data" / "training-manifest.json"

def main() -> None:
    api = HfApi()
    # Non-recursive: list only files in the date folder
    entries = api.list_repo_tree(
        repo_id=REPO_ID,
        path=DATE_FOLDER,
        repo_type="dataset",
        recursive=False,
    )

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        cdn_url = (
            f"https://huggingface.co/datasets/{REPO_ID}"
            f"/resolve/main/{DATE_FOLDER}/{entry.path}"
        )
        files.append(
            {
                "filename": entry.path,
                "date_folder": DATE_FOLDER,
                "cdn_url": cdn_url,
                "size": getattr(entry, "size", None),
                "local_path": str(
                    Path(os.getenv("HF_DATASETS_CACHE", "~/.cache/huggingface/datasets"))
                    / "datasets"
                    / REPO_ID
                    / "data"
                    / DATE_FOLDER
                    / entry.path
                ),
            }
        )

    if not files:
        raise RuntimeError(f"No files found in {REPO_ID}/{DATE_FOLDER}")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_id": REPO_ID,
        "date_folder": DATE_FOLDER,
        "file_count": len(files),
        "files": files,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"✅ Manifest written to {OUTPUT_PATH} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x scripts/generate-training-manifest.py
```

---

### `surrogate/train.py` (key excerpt)
```python
import json
import os
from pathlib import Path

import torch
from datasets import load_dataset
from lightning import Fabric
from transformers import AutoTokenizer, AutoModelForCausalLM

MANIFEST_PATH = Path(__file__).parent.parent / "data" / "training-manifest.json"

def load_cdn_dataset(manifest_path: Path):
    """Load dataset using manifest; prefer local cache, fallback to CDN URLs."""
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    # Prefer local files if available and non-empty
    data_files = []
    for item in manifest["files"]:
        local_path = Path(item.get("local_path", ""))
        if local_path.exists() and local_path.stat().st_size > 0:
            data_files.append(str(local_path))
        else:
            data_files.append(item["cdn_url"])

    ds = load_dataset(
        "json",
        data_files=data_files,
        streaming=False,
        split="train",
    )

    # Project to surrogate-1 schema
    def _project(item):
        return {
            "prompt": item.get("prompt") or item.get("input") or "",
            "response": item.get("response") or item.get("output") or "",
        }

    ds = ds.map(_project, remove_columns=list(ds.features.keys()))
    return ds

def train_step(fabric, model, tokenizer, batch):
    inputs = tokenizer(
        batch["prompt"],
        return_tensors="pt",
        truncation=True,
        padding="max_length",
        max_length=512,
    )
    labels = tokenizer(
        batch["response"],
        return_tensors="pt",
        truncation=True,
        padding="max_length",
        max_length=512,
    )
    outputs = model(**inputs, labels=labels["input_ids"])
    loss = outputs.loss
    fabric.backward(loss)
    return loss
```

---

### `scripts/run_training.sh`
```bash
#!/bin/bash
# Robust launcher: studio reuse + auto-restart + manifest generation
set -euo pipefail
SHELL=/bin/bash

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST_PATH="${PROJECT_ROOT}/data/training-manifest.json"
MANIFEST_SCRIPT="${PROJECT_ROOT}/scripts/generate-training-manifest.py"
TRAIN_SCRIPT="${PROJECT_ROOT}/surrogate/train.py"

# Optional: regenerate manifest if missing or older than 24h
if [[ ! -f "${MANIFEST_PATH}" ]] || \
   [[ $(find "${MANIFEST_PATH}" -mmin +1440 -print) ]]; then
  echo "🔄 Generating training manifest..."
  python3 "${MANIFEST_SCRIPT}"
else
  echo "✅ Using existing manifest: ${MANIFEST_PATH}"
fi

# Studio health check + restart helper (customize to your infra)
wait_for_studio() {
  local max_wait=300 step=10 elapsed=0
  while ! python3 -c "import lightning as L; studio = L.LightningStudio(); studio.is_running()" 2>/dev/null; do
    if (( elapsed >= max_wait )); then
      echo "⚠️ Studio not healthy after ${max_wait}s; attempting restart..."
      # Insert your studio restart command here, e.g.:
      #
