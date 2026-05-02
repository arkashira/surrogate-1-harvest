# vanguard / quality

## Final Synthesized Solution

### Diagnosis (Consolidated)
1. **HF CDN-bypass missing** — no `file-list.json` forces `load_dataset`/`hf` API calls during training, causing 429s on heterogeneous datasets.
2. **Lightning Studio non-idempotent** — runs create or start studios blindly, risking quota waste and idle-stop deaths (~80 hr/mo).
3. **No canonical Mac-only orchestration entrypoint** — absence of a single `make train`/`orchestrate.sh` that enforces “Mac orchestrates only; Lightning trains remotely.”
4. **Missing top-hub/knowledge-rag review** — no pre-check to surface MOC/top-hub context before training.
5. **Dataset-mirror projection not idempotent** — mixed-schema parquet written to wrong location instead of `batches/mirror-merged/{date}/{slug}.parquet` with strict `{prompt,response}` projection.

---

### Proposed Change (Single Source of Truth)
Create `/opt/axentx/vanguard/orchestrate/train_surrogate1.py` as the **single orchestration entrypoint** that:
- Lists one date folder via HF API **once**, writes `file-list.json` to repo root for CDN-only fetches.
- Reuses or idempotently starts a Lightning Studio (L40S, `lightning-public-prod`), never creating duplicates.
- Submits a remote training run that uses **CDN-only fetches** (zero HF API calls during data load).
- Prints top-hub MOC from knowledge-rag if available (non-blocking).
- Enforces Mac-only orchestration; all training happens on Lightning.

Add `/opt/axentx/vanguard/orchestrate/Makefile` with `make train` and `make clean`.

Add `/opt/axentx/vanguard/orchestrate/orchestrate.sh` as a POSIX-friendly wrapper for environments without `make`.

Add dataset mirror helper to project to `batches/mirror-merged/{date}/{slug}.parquet` with `{prompt,response}` only.

---

### Implementation

#### 1) Orchestration script
`/opt/axentx/vanguard/orchestrate/train_surrogate1.py`
```python
#!/usr/bin/env python3
"""
Orchestrate Surrogate-1 training with HF CDN-bypass and Lightning Studio reuse.
Mac-only orchestration; training runs remotely on Lightning.
"""
import json, os, sys, time, subprocess
from pathlib import Path

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate1-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")
FILE_LIST = Path(__file__).parent.parent / "file-list.json"
LIGHTNING_NAME = "vanguard-surrogate1-l40s"

def _ensure_pkg(pkg, import_name=None):
    import_name = import_name or pkg
    try:
        __import__(import_name)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

def list_hf_files():
    """Single API call to list files in DATE_FOLDER; save for CDN-bypass."""
    _ensure_pkg("huggingface_hub")
    from huggingface_hub import list_repo_tree

    items = list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    files = [f.rfilename for f in items if f.rfilename.endswith(".parquet")]
    FILE_LIST.write_text(json.dumps(files, indent=2))
    print(f"Saved {len(files)} files to {FILE_LIST}")
    return files

def top_hub_moc():
    """Print MOC hub insight if available (non-blocking)."""
    try:
        kb_path = Path(__file__).parent.parent / "knowledge_rag" / "top_hubs.json"
        if kb_path.exists():
            data = json.loads(kb_path.read_text())
            print("Top hub (MOC):", data.get("MOC", "N/A"))
    except Exception:
        pass

def reuse_or_start_studio():
    """Idempotent Lightning Studio reuse; restart if stopped."""
    _ensure_pkg("lightning")
    from lightning_sdk import Client, Machine, Teamspace

    client = Client()
    teamspace = Teamspace.current()
    studios = [s for s in teamspace.studios if s.name == LIGHTNING_NAME]

    if studios:
        studio = studios[0]
        if studio.status == "running":
            print(f"Reusing running studio: {studio.name}")
            return studio
        if studio.status == "stopped":
            print(f"Restarting stopped studio: {studio.name}")
            studio.start(machine=Machine.L40S, cloud="lightning-public-prod")
            return studio
    else:
        print(f"Creating studio: {LIGHTNING_NAME}")
        studio = teamspace.studios.create(
            name=LIGHTNING_NAME,
            machine=Machine.L40S,
            cloud="lightning-public-prod",
        )
        return studio

def submit_training(studio):
    """Submit train.py that uses CDN-only fetches via file-list.json."""
    train_script = Path(__file__).parent / "train_surrogate1.py"
    if not train_script.exists():
        train_script.write_text(_default_train_script())

    run = studio.runs.create(
        name="surrogate1-train",
        script=str(train_script.relative_to(Path(__file__).parent)),
        config={
            "env": {
                "HF_DATASET_REPO": HF_REPO,
                "FILE_LIST": str(FILE_LIST),
            }
        },
    )
    print(f"Submitted run: {run.name} (id={run.id})")
    return run

def _default_train_script() -> str:
    return '''#!/usr/bin/env python3
"""
Lightning training script: uses CDN-only fetches (zero HF API calls during data load).
Expects FILE_LIST env var pointing to JSON list of parquet files.
"""
import os, json, torch
from torch.utils.data import Dataset, DataLoader
import pyarrow.parquet as pq
import requests

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate1-mirror")
FILE_LIST = os.getenv("FILE_LIST", "file-list.json")
CDN_ROOT = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

class CDNParquetDataset(Dataset):
    def __init__(self, files, max_samples=None):
        if isinstance(files, str):
            with open(files) as f:
                files = json.load(f)
        self.files = files if isinstance(files, list) else []
        if max_samples:
            self.files = self.files[:max_samples]

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        url = f"{CDN_ROOT}/{self.files[idx]}"
        local = requests.get(url, timeout=30)
        local.raise_for_status()
        tbl = pq.read_table(local.content)
        prompt = tbl.column("prompt").to_pylist()[0] if "prompt" in tbl.column_names else ""
        response = tbl.column("response").to_pylist()[0] if "response" in tbl.column_names else ""
        return {"prompt": prompt, "response": response}

if __name__ == "__main__":
    ds = CDNParquetDataset(FILE_LIST, max_samples=1000)
    loader = DataLoader(ds, batch_size=8, shuffle=True)
    for batch in loader:
        # Replace with real surrogate-1 training step
        if batch["prompt"]:
            print(batch["prompt"][0][:60])
'''

def main():
    top_hub_moc()
    files = list_hf_files()
    if not files:
        print("No parquet files found; aborting.")
        sys.exit(1)

    studio = reuse_or_start_studio()
    if studio.status != "running":
        print("Waiting for studio to be running...")
        time.sleep(30)
        studio = reuse_or_start_studio()

    run = submit_training(studio)
    print("Training submitted. Monitor via Lightning Studio.")

if __name__ == "__main__":
    main()
```

#### 2) Training script used by the run
`/opt/axentx/vanguard/orchestrate/train_surrogate1.py` (see above — the orchestrator writes it if missing, but keep it in repo).

#### 3) Dataset mirror helper (idempotent
