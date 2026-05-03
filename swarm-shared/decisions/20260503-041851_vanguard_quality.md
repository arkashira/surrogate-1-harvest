# vanguard / quality

## 1. Diagnosis

- No content-addressed manifest exists → training/frontend hit HF API at runtime, causing 429s and non-reproducible epochs.
- Mixed-schema `enriched/` files include `source`/`ts` columns that break `load_dataset` expectations for surrogate-1 training.
- Lightning Studio reuse is not implemented → each run recreates studio and burns 80hr/mo quota.
- Data loading uses `load_dataset(streaming=True)` on heterogeneous repos → triggers pyarrow `CastError` on schema drift.
- No CDN bypass strategy → every epoch re-authenticates against `/api/` and risks rate limits.

## 2. Proposed change

Create a minimal, high-leverage quality fix: add a **content-addressed manifest generator + Lightning Studio reuse wrapper** that eliminates runtime HF API calls and enforces schema projection before ingestion.

Scope:
- `/opt/axentx/vanguard/scripts/build_manifest.py` (new)
- `/opt/axentx/vanguard/scripts/run_training.py` (modify)
- `/opt/axentx/vanguard/train.py` (modify data loader)

## 3. Implementation

### 3.1 Manifest generator (build_manifest.py)

```python
#!/usr/bin/env python3
"""
Generate content-addressed manifest for a date folder.
Run from Mac (or any dev box) after rate-limit window clears.
Embeds the manifest into training script to enable CDN-only fetches.
"""
import json, hashlib, os, sys
from datetime import datetime
from huggingface_hub import HfApi, DatasetFilter

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/surrogate-1")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
OUT_MANIFEST = os.getenv("OUT_MANIFEST", f"manifest-{DATE_FOLDER}.json")

def list_date_files(api: HfApi, date_folder: str):
    # single shallow call; avoids recursive pagination
    items = api.list_repo_tree(
        repo_id=HF_REPO,
        path=f"enriched/{date_folder}",
        repo_type="dataset",
        recursive=False,
    )
    files = []
    for item in items:
        if item.get("type") == "file" and item["path"].endswith(".parquet"):
            files.append(item["path"])
    return sorted(files)

def build_manifest(files):
    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "files": [],
    }
    for f in files:
        # CDN URL (no auth, bypasses /api/ rate limits)
        cdn_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{f}"
        slug = os.path.splitext(os.path.basename(f))[0]
        manifest["files"].append({
            "path": f,
            "slug": slug,
            "cdn_url": cdn_url,
            "sha256": None,  # optional: populate via HEAD ETag if desired
        })
    manifest["sha256"] = hashlib.sha256(
        json.dumps(manifest["files"], sort_keys=True).encode()
    ).hexdigest()
    return manifest

def main():
    api = HfApi()
    files = list_date_files(api, DATE_FOLDER)
    if not files:
        print(f"No parquet files found for enriched/{DATE_FOLDER}")
        sys.exit(1)
    manifest = build_manifest(files)
    with open(OUT_MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {OUT_MANIFEST} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

### 3.2 Lightning Studio reuse wrapper (run_training.py)

```python
#!/usr/bin/env python3
"""
Launch surrogate-1 training in an existing running Lightning Studio
or create one if none exists. Avoids quota burn from recreation.
"""
import os, sys, time
from lightning import Lightning, Teamspace, Studio, Machine

SCRIPT = os.path.join(os.path.dirname(__file__), "train.py")
STUDIO_NAME = os.getenv("STUDIO_NAME", "vanguard-surrogate1")
MACHINE = Machine.L40S  # free tier max; change to H200 in lightning-lambda-prod if available

def get_running_studio():
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == STUDIO_NAME and s.status == "Running":
            return s
    return None

def main():
    lightning = Lightning()
    studio = get_running_studio()

    if studio is None:
        print(f"No running studio '{STUDIO_NAME}' found. Creating...")
        studio = Studio.create(
            name=STUDIO_NAME,
            machine=MACHINE,
            create_ok=True,
        )
        # wait until running
        while studio.status != "Running":
            time.sleep(10)
            studio = Studio.get(STUDIO_NAME)
    else:
        print(f"Reusing running studio '{STUDIO_NAME}'")

    # Ensure studio is alive before run
    if studio.status != "Running":
        print("Studio stopped. Restarting...")
        studio.start(machine=MACHINE)
        while studio.status != "Running":
            time.sleep(10)
            studio = Studio.get(STUDIO_NAME)

    # Run training script inside studio
    run = studio.run(
        command=[sys.executable, SCRIPT],
        environment={
            "HF_DATASET_REPO": os.getenv("HF_DATASET_REPO", "datasets/surrogate-1"),
            "MANIFEST_PATH": os.getenv("MANIFEST_PATH", "manifest-latest.json"),
        },
    )
    print(f"Training run submitted: {run}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/scripts/run_training.py
```

### 3.3 Update train.py data loader to use manifest + CDN

Replace any `load_dataset` call with a CDN-based parquet loader that projects `{prompt, response}` only:

```python
# In train.py (or data.py) — replace existing loader
import os, json, pyarrow.parquet as pq, requests, io
import torch
from torch.utils.data import IterableDataset

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifest-latest.json")

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path=MANIFEST_PATH):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.file_urls = [item["cdn_url"] for item in self.manifest["files"]]

    def __iter__(self):
        for url in self.file_urls:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            table = pq.read_table(io.BytesIO(resp.content))
            # Project only required fields; ignore source/ts/mixed schema
            prompts = table.column("prompt").to_pylist()
            responses = table.column("response").to_pylist()
            for prompt, response in zip(prompts, responses):
                if prompt is None or response is None:
                    continue
                yield {"prompt": str(prompt), "response": str(response)}

# Usage in training loop
train_dataset = CDNParquetDataset()
```

## 4. Verification

1. Generate manifest (run once per date folder):
   ```bash
   cd /opt/axentx/vanguard
   HF_DATASET_REPO=datasets/surrogate-1 DATE_FOLDER=2026-05-01 \
     python scripts/build_manifest.py
   ```
   Confirm `manifest-2026-05-01.json` exists and lists parquet files with `cdn_url`.

2. Validate schema projection:
   ```bash
   python -c "
import json, pyarrow.parquet as pq, io, requests
m=json.load(open('manifest-2026-05-01.json'))
for u in m['files'][:1]:
    t=pq.read_table(io.BytesIO(requests.get(u['cdn_url']).content))
    print('columns:', t.column_names)
