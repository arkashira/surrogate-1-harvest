# airship / frontend

## Final Synthesis — Highest-Value Incremental Improvement (≤2h)

**Goal:**  
Embed a CDN-only file manifest into the Surrogate-1 training pipeline so Lightning Studio training runs with **zero HF API calls during data loading**, eliminating 429 rate-limit risk and quota burn during iteration.

**Why this wins:**
- Uses existing HF CDN bypass (`resolve/main/` URLs require no auth and avoid `/api/`).
- Single manifest generation + small loader patch; no schema, model, or infra changes.
- Safe: deterministic, reproducible, and includes a fallback to legacy behavior if manifest is missing.
- Fits comfortably in <2h and immediately hardens iteration velocity.

---

## Concrete Implementation Plan (≤2h)

### 1) Locate training entrypoint
- Identify where Surrogate-1 loads data (e.g., `surrogate/train.py` or `surrogate/data/train_loader.py`).

---

### 2) Add manifest generator (`surrogate/scripts/build_file_manifest.py`)
- Runs on Mac orchestrator (or CI) after dataset updates.
- Targets a single date folder (e.g., `batches/mirror-merged/2026-05-03/`).
- Uses `list_repo_tree(..., recursive=False)` to list files.
- Emits `file_manifest.json`:
  ```json
  {
    "repo": "your-org/surrogate-1-dataset",
    "date": "2026-05-03",
    "root": "batches/mirror-merged/2026-05-03",
    "files": [
      {"path": "file1.parquet", "url": "https://huggingface.co/datasets/your-org/surrogate-1-dataset/resolve/main/batches/mirror-merged/2026-05-03/file1.parquet", "size": 12345},
      ...
    ]
  }
  ```
- Commits or copies `file_manifest.json` into the repo/Docker context so training can read it locally.

**Generator script (concise, correct):**
```python
# surrogate/scripts/build_file_manifest.py
import json
import os
from pathlib import Path
from huggingface_hub import list_repo_tree

REPO = os.getenv("REPO", "your-org/surrogate-1-dataset")
DATE_DIR = os.getenv("DATE_DIR", "batches/mirror-merged/2026-05-03")
OUTFILE = Path(os.getenv("OUTFILE", "file_manifest.json"))

tree = list_repo_tree(repo_id=REPO, path=DATE_DIR, recursive=False)
files = []
for item in tree:
    if item.type == "file":
        files.append({
            "path": f"{DATE_DIR}/{item.path.split('/')[-1]}",
            "url": f"https://huggingface.co/datasets/{REPO}/resolve/main/{DATE_DIR}/{item.path.split('/')[-1]}",
            "size": item.size
        })

manifest = {
    "repo": REPO,
    "date": DATE_DIR.split('/')[-1],
    "root": DATE_DIR,
    "files": files
}

OUTFILE.write_text(json.dumps(manifest, indent=2))
print(f"Wrote {len(files)} files to {OUTFILE}")
```

---

### 3) Update training loader (safe, CDN-first)
- At loader init, try to load `file_manifest.json`.
- If present: build URLs and use `datasets` with `data_files=[urls]` (or `parquet` loader) pointing to CDN URLs; project to `{prompt, response}`.
- If absent: fall back to legacy `load_dataset` path unchanged (no breakage).

**Loader patch (minimal, correct):**
```python
# surrogate/data/train_loader.py  (or train.py excerpt)
import json
import os
from pathlib import Path
from datasets import load_dataset, Dataset

MANIFEST_PATH = Path(os.getenv("CDN_MANIFEST", "file_manifest.json"))
CACHE_DIR = Path(os.getenv("HF_CACHE", ".cache"))
CACHE_DIR.mkdir(exist_ok=True)

def project_to_prompt_response(example):
    return {"prompt": example["prompt"], "response": example["response"]}

def build_dataset(max_samples=None):
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text())
        urls = [f["url"] for f in manifest["files"]]
        # CDN-only: datasets will stream from resolve/main/ without API auth
        ds = load_dataset("parquet", data_files=urls, split="train", cache_dir=str(CACHE_DIR))
    else:
        # Fallback: legacy loader (no behavior change)
        ds = load_dataset(manifest.get("repo", ""), name=None, split="train", cache_dir=str(CACHE_DIR))

    ds = ds.map(project_to_prompt_response, remove_columns=[c for c in ds.column_names if c not in {"prompt", "response"}])
    if max_samples:
        ds = ds.select(range(min(max_samples, len(ds))))
    return ds

# Example usage
if __name__ == "__main__":
    ds = build_dataset(max_samples=4)
    print("Columns:", ds.column_names)
    print("Sample:", ds[0] if len(ds) else "empty")
```

---

### 4) Lightning Studio integration (reuse + zero-API guard)
- Ensure `file_manifest.json` is included in the Docker context or repo copy used by Studio.
- Reuse a running Studio if available; otherwise start one.
- Submit job with manifest path and confirm zero HF API calls during data loading.

**Launcher snippet (corrected):**
```python
# launch_studio.py
from lightning import Lightning, Teamspace, Machine, Studio

teamspace = Teamspace()
running = None
for s in teamspace.studios:
    if s.name == "surrogate-1-train" and s.status == "Running":
        running = s
        break

if running is None:
    studio = Studio.create(name="surrogate-1-train", machine=Machine.L40S)
else:
    studio = running

if studio.status != "Running":
    studio.start(machine=Machine.L40S)

studio.run(
    entry_point="python",
    arguments=["train.py", "--epochs", "1"],
    env={"CDN_MANIFEST": "file_manifest.json", "HF_CACHE": ".cache"},
)
```

---

### 5) Validation (≤15 min)
- Dry-run locally: `python train.py --dry-run` should list files via manifest and show CDN URLs.
- Confirm no `huggingface_hub` API list calls (check logs or mock).
- In Studio, verify data loading from CDN URLs and **zero** HF API 429 errors.
- Training iteration completes one step without quota burn.

---

## Acceptance Criteria
- `file_manifest.json` committed and contains correct CDN URLs for the target date folder.
- `train.py`/loader does **not** call `list_repo_files` or `load_dataset(streaming=True)` on heterogeneous repo during training when manifest is present.
- Lightning Studio run logs show data loading from CDN URLs and **zero** HF API 429 errors.
- Training iteration completes one step in Studio without quota burn.
