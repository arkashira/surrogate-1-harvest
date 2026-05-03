# airship / discovery

## Implementation Plan (≤2h)

**Highest-value incremental improvement:** Deterministic CDN file manifest + Lightning Studio lifecycle resilience for Surrogate-1 training.

**Why:** Eliminates HF API 429s during Surrogate-1 training and prevents idle-timeout deaths of long-running Lightning jobs.

---

### 1) Pre-list date folder → JSON manifest (Mac orchestration)
Single `list_repo_tree(recursive=False)` for one date folder → JSON saved to repo. Lightning training uses CDN-only fetches with zero API calls during data load.

**File:** `/opt/axentx/airship/surrogate/scripts/generate_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic CDN manifest for Surrogate-1 training.
Run from Mac (or any orchestration host) after rate-limit window clears.
"""
import json
import os
import sys
from datetime import datetime
from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-1-batches"  # adjust as needed
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")  # e.g. batches/mirror-merged/2026-04-29
OUT_PATH = os.getenv("OUT_MANIFEST", f"surrogate/manifests/{DATE_FOLDER}.json")

def main() -> None:
    api = HfApi()
    folder_path = f"batches/mirror-merged/{DATE_FOLDER}"
    print(f"Listing {REPO_ID}/{folder_path} ...")

    try:
        items = api.list_repo_tree(
            repo_id=REPO_ID,
            path=folder_path,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as e:
        print(f"HF API error: {e}", file=sys.stderr)
        sys.exit(1)

    files = [
        {
            "path": f"{folder_path}/{item.path.split('/')[-1]}",
            "filename": item.path.split("/")[-1],
            "cdn_url": f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{folder_path}/{item.path.split('/')[-1]}",
            "size": getattr(item, "size", None),
        }
        for item in items
        if not item.path.endswith("/")  # skip subfolders
    ]

    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "repo_id": REPO_ID,
        "folder": folder_path,
        "count": len(files),
        "files": files,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files -> {OUT_PATH}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x /opt/axentx/airship/surrogate/scripts/generate_manifest.py
```

---

### 2) Lightning training data loader (CDN-only)
Embed manifest; fetch via CDN with retry/backoff. Zero HF API calls during training.

**File:** `/opt/axentx/airship/surrogate/train/data_loader.py`

```python
import json
import os
from typing import Dict, List
import requests
from datasets import Dataset, Features, Value

MANIFEST_PATH = os.getenv(
    "MANIFEST_PATH",
    "surrogate/manifests/2026-04-29.json",
)

CDN_TIMEOUT = int(os.getenv("CDN_TIMEOUT", "60"))
CDN_MAX_RETRIES = int(os.getenv("CDN_MAX_RETRIES", "5"))

def load_manifest(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def stream_cdn_parquet_rows(manifest: Dict):
    """Yield rows from CDN parquet files without HF API auth."""
    import pyarrow.parquet as pq
    import pyarrow as pa
    import io

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=CDN_MAX_RETRIES)
    session.mount("https://", adapter)

    for entry in manifest["files"]:
        if not entry["filename"].endswith(".parquet"):
            continue
        url = entry["cdn_url"]
        print(f"Fetching {url}")
        resp = session.get(url, timeout=CDN_TIMEOUT)
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        table = pq.read_table(buf)
        # Project to {prompt, response} only (schema resilience)
        cols = {k: table[k] for k in ("prompt", "response") if k in table.column_names}
        if len(cols) != 2:
            print(f"Skipping {entry['filename']}: missing prompt/response")
            continue
        proj = pa.table(cols)
        for batch in proj.to_batches(max_chunksize=1000):
            for i in range(batch.num_rows):
                yield {
                    "prompt": batch["prompt"][i].as_py(),
                    "response": batch["response"][i].as_py(),
                }

def build_dataset(manifest_path: str = MANIFEST_PATH) -> Dataset:
    manifest = load_manifest(manifest_path)
    features = Features(
        {
            "prompt": Value("string"),
            "response": Value("string"),
        }
    )
    return Dataset.from_generator(
        lambda: stream_cdn_parquet_rows(manifest),
        features=features,
    )
```

---

### 3) Lightning Studio lifecycle resilience
Reuse running studios; restart automatically on idle-stop to avoid quota waste and training death.

**File:** `/opt/axentx/airship/surrogate/train/lightning_orchestrator.py`

```python
#!/usr/bin/env python3
import os
import time
from lightning_sdk import Client, Studio, Machine
from lightning_sdk.workspaces import Workspace

API_KEY = os.getenv("LIGHTNING_API_KEY")
TEAMSPACE = os.getenv("LIGHTNING_TEAMSPACE", "default")
STUDIO_NAME = os.getenv("LIGHTNING_STUDIO_NAME", "surrogate-1-train")
MACHINE_TYPE = Machine.L40S  # fallback to free-tier L40S if H200 unavailable

client = Client(api_key=API_KEY)
workspace = Workspace(client, name=TEAMSPACE)

def get_or_create_studio() -> Studio:
    for s in workspace.studios:
        if s.name == STUDIO_NAME:
            if s.status == "running":
                print(f"Reusing running studio: {STUDIO_NAME}")
                return s
            elif s.status == "stopped":
                print(f"Restarting stopped studio: {STUDIO_NAME}")
                s.start(machine=MACHINE_TYPE)
                return s
    print(f"Creating new studio: {STUDIO_NAME}")
    return workspace.studios.create(
        STUDIO_NAME,
        machine=MACHINE_TYPE,
        container_image="pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime",
    )

def run_training_script(script_path: str, args: List[str]):
    studio = get_or_create_studio()
    # Ensure studio is running before submit
    if studio.status != "running":
        studio.start(machine=MACHINE_TYPE)
        time.sleep(10)  # brief warm-up

    run = studio.runs.create(
        script_path,
        arguments=args,
        environment={
            "MANIFEST_PATH": "surrogate/manifests/2026-04-29.json",
            "HF_HOME": "/tmp/hf_home",  # avoid Mac ~/.cache collisions
        },
    )
    print(f"Submitted run {run.name} (id={run.id})")
    return run

if __name__ == "__main__":
    # Example usage
    run_training_script(
        "train/train_surrogate.py",
        ["--epochs", "3", "--batch-size", "16"],
    )
```

---

### 4) Cron / systemd safety (wrapper shebang fix)
Ensure all wrapper scripts invoked via Bash with proper shebang and executable bit.

Example wrapper: `/opt/axentx/airship/surrogate/scripts/train_wrapper.sh`

```bash
