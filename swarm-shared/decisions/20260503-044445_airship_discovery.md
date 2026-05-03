# airship / discovery

## Highest-Value Incremental Improvement (≤2h)

**Goal:** Make Surrogate-1 training resilient to HF API 429s and Lightning Studio idle-stop by implementing deterministic CDN-only data loading + Studio lifecycle reuse.

**Why:**  
- Eliminates HF API rate-limit failures during data loads (uses CDN URLs instead of `/api/`).  
- Prevents quota loss from idle-stop/studio recreation by reusing running studios and restarting only when stopped.  
- Fits within 2h: focused changes to data loader + training launcher.

---

## Implementation Plan

### 1. Pre-list file paths once (Mac orchestration)
- Run `list_repo_tree` for the target date folder (non-recursive) and save to `file_list.json`.
- Embed `file_list.json` in the training repo so Lightning training uses CDN-only fetches with zero API calls during data load.

### 2. CDN-only dataset loader
- Replace `load_dataset(streaming=True)` with manual CDN downloads via `hf_hub_download` (or raw CDN URLs) for files in `file_list.json`.
- Project each file to `{prompt, response}` only at parse time (ignore mixed schemas).
- Use `datasets.Dataset.from_generator` to stream parsed examples without loading all files into memory.

### 3. Lightning Studio lifecycle wrapper
- Before `Studio(create_ok=True)`, list `Teamspace.studios` and reuse any running studio with matching name.
- If studio exists but is stopped, restart it with `target.start(machine=Machine.L40S)`.
- Guard each `.run()` with status check; restart if stopped.

### 4. Training script integration
- Accept `file_list.json` path as CLI arg.
- Use deterministic shard assignment (hash slug → sibling repo) for future multi-repo writes (optional, no immediate change).

---

## Code Snippets

### 1. Pre-list file paths (run on Mac)
```bash
# install lightning-sdk if needed
pip install lightning
```

```python
# scripts/list_files_for_date.py
import json
from lightning import Lightning

def list_files_for_date(repo: str, date_folder: str, output_path: str):
    api = Lightning()
    tree = api.list_repo_tree(repo, path=date_folder, recursive=False)
    # tree format: [{"path": "folder/file1.parquet"}, ...]
    paths = [item["path"] for item in tree if item["path"].endswith(".parquet")]
    with open(output_path, "w") as f:
        json.dump({"repo": repo, "date_folder": date_folder, "files": paths}, f, indent=2)
    print(f"Saved {len(paths)} files to {output_path}")

if __name__ == "__main__":
    import sys
    repo = sys.argv[1]
    date_folder = sys.argv[2]
    output_path = sys.argv[3]
    list_files_for_date(repo, date_folder, output_path)
```

Run once (after rate-limit window clears):
```bash
python scripts/list_files_for_date.py datasets/my-repo 2026-04-29 file_list.json
```

---

### 2. CDN-only dataset loader
```python
# surrogate/data/cdn_loader.py
import json
import os
from pathlib import Path
from typing import Iterator, Dict, Any
from datasets import Dataset
from huggingface_hub import hf_hub_download

def cdn_dataset_generator(file_list_path: str, cache_dir: str = ".cache") -> Iterator[Dict[str, Any]:
    with open(file_list_path) as f:
        manifest = json.load(f)
    repo = manifest["repo"]
    files = manifest["files"]

    os.makedirs(cache_dir, exist_ok=True)

    for rel_path in files:
        # CDN download (bypasses API rate limits)
        local_path = hf_hub_download(
            repo_id=repo,
            filename=rel_path,
            cache_dir=cache_dir,
            local_files_only=False,
        )
        # Project to {prompt, response} only
        # Replace with your actual projection logic (e.g., pyarrow read)
        import pyarrow.parquet as pq
        table = pq.read_table(local_path)
        df = table.select(["prompt", "response"]).to_pandas()
        for _, row in df.iterrows():
            yield {"prompt": row["prompt"], "response": row["response"]}

def load_cdn_dataset(file_list_path: str) -> Dataset:
    return Dataset.from_generator(
        cdn_dataset_generator,
        gen_kwargs={"file_list_path": file_list_path},
        cache_dir=".cache",
    )
```

---

### 3. Lightning Studio lifecycle wrapper
```python
# surrogate/train/lightning_studio.py
from lightning import Lightning, Teamspace, Studio, Machine
import time

def get_or_create_studio(
    studio_name: str,
    machine: Machine = Machine.L40S,
    idle_timeout_minutes: int = 120,
) -> Studio:
    api = Lightning()
    teamspace = Teamspace()

    # Reuse running studio
    for s in teamspace.studios:
        if s.name == studio_name and s.status == "running":
            print(f"Reusing running studio: {studio_name}")
            return s

    # If stopped, restart
    for s in teamspace.studios:
        if s.name == studio_name and s.status == "stopped":
            print(f"Restarting stopped studio: {studio_name}")
            s.start(machine=machine)
            # Wait until running
            while s.status != "running":
                time.sleep(10)
                s.refresh()
            return s

    # Create new
    print(f"Creating new studio: {studio_name}")
    return Studio(
        name=studio_name,
        machine=machine,
        create_ok=True,
        idle_timeout_minutes=idle_timeout_minutes,
    )

def run_training_with_studio(
    studio_name: str,
    script_path: str,
    script_args: list,
    machine: Machine = Machine.L40S,
):
    studio = get_or_create_studio(studio_name, machine=machine)

    # Guard against idle-stop during long runs
    def ensure_running():
        studio.refresh()
        if studio.status != "running":
            print(f"Studio stopped. Restarting...")
            studio.start(machine=machine)
            while studio.status != "running":
                time.sleep(10)
                studio.refresh()

    # Run training
    ensure_running()
    run = studio.run(script_path, script_args)
    return run
```

---

### 4. Updated training script entrypoint
```python
# surrogate/train/train_surrogate.py
import argparse
from pathlib import Path
from ..data.cdn_loader import load_cdn_dataset
from ..model.surrogate_trainer import train_step  # your training loop

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list", required=True, help="Path to file_list.json")
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    print("Loading CDN dataset...")
    dataset = load_cdn_dataset(args.file_list)
    print(f"Loaded {len(dataset)} examples")

    Path(args.output_dir).mkdir(exist_ok=True)

    for epoch in range(args.epochs):
        print(f"Epoch {epoch+1}/{args.epochs}")
        for batch in dataset.iter(batch_size=8):
            train_step(batch, output_dir=args.output_dir)

    print("Training complete")

if __name__ == "__main__":
    main()
```

---

### 5. Launcher script (orchestration)
```bash
#!/usr/bin/env bash
# scripts/run_training.sh
set -euo pipefail

export SHELL=/bin/bash

FILE_LIST="file_list.json"
STUDIO_NAME="surrogate-training-l40s"

# If file_list doesn't exist, generate it (run once manually after rate-limit clears)
if [[ ! -f "$FILE_LIST" ]]; then
    echo "file_list.json not found. Generate it first with list_files_for_date.py"
    exit 1
fi

python -m surrogate.train.lightning_studio \
    --studio-name "$STUDIO_NAME" \
    --script-path surrogate/train/train_surrogate.py \
    --script-args "--file-list=$FILE_LIST" "--epochs=1"
```

Make executable:
```bash
chmod +x scripts/run_training.sh
