# surrogate-1 / quality

## Highest-Value Improvement

Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables Lightning Studio to do CDN-only fetches with zero HF API calls during data load — eliminating 429s and saving quota.

## Implementation Plan (≤2h)

1. **Create snapshot generator** (`bin/snapshot.sh`)  
   - Uses `huggingface_hub` to `list_repo_tree(path, recursive=False)` for a single date folder  
   - Outputs `batches/public-merged/<date>/files.json` with CDN-ready paths  
   - Idempotent; skips if snapshot already exists for the date  
   - Sets `SHELL=/bin/bash`, includes shebang, `chmod +x`

2. **Embed manifest in training script** (`train.py` or launcher)  
   - Load `files.json` at startup  
   - Build URLs: `f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{path}"`  
   - Use `torchdata`/`webdataset`/`IterableDataset` to stream via CDN (no `load_dataset` or `hf api` during training)  
   - Validate one file first; fail fast if 404

3. **Reuse existing Lightning Studio**  
   - Before `.run()`, list `Teamspace.studios`, reuse running studio with name match  
   - If stopped, restart with `target.start(machine=Machine.L40S)`  

4. **Update workflow** (optional)  
   - Add step to run `bin/snapshot.sh` before training job  
   - Pass date via env var; snapshot becomes build artifact

---

## Code Snippets

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

# surrogate-1 snapshot generator
# Lists dataset files for a single date folder and emits CDN manifest.
# Usage: HF_TOKEN=... bin/snapshot.sh <date>  # e.g. 2026-05-03

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT_DIR="batches/public-merged/${DATE}"
OUT_FILE="${OUT_DIR}/files.json"

mkdir -p "${OUT_DIR}"

# Skip if snapshot already exists
if [[ -f "${OUT_FILE}" ]]; then
  echo "Snapshot already exists: ${OUT_FILE}"
  exit 0
fi

python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi

HF_TOKEN = os.environ.get("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

api = HfApi(token=HF_TOKEN)
repo = "${REPO}"
date = "${DATE}"
folder = f"batches/public-merged/{date}"

try:
    items = api.list_repo_tree(repo=repo, path=folder, recursive=False)
except Exception as e:
    print(f"Failed to list repo tree: {e}", file=sys.stderr)
    sys.exit(1)

files = [
    {"path": it.path, "size": getattr(it, "size", None)}
    for it in items
    if it.type == "file"
]

out = {
    "date": date,
    "folder": folder,
    "files": files,
    "cdn_base": "https://huggingface.co/datasets"
}

with open("${OUT_FILE}", "w") as f:
    json.dump(out, f, indent=2)

print(f"Wrote {len(files)} files to ${OUT_FILE}")
PY
```

### Minimal `train.py` snippet (CDN-only loader)
```python
import json, os, io, torch
from torch.utils.data import IterableDataset
import requests

class CDNParquetIterable(IterableDataset):
    def __init__(self, manifest_path, repo):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.repo = repo
        self.base = self.manifest["cdn_base"]

    def _stream_file(self, path):
        url = f"{self.base}/{self.repo}/resolve/main/{path}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return io.BytesIO(resp.content)

    def __iter__(self):
        for item in self.manifest["files"]:
            buf = self._stream_file(item["path"])
            # decode parquet -> {prompt, response} projection here
            # yield {"prompt": ..., "response": ...}
            # (use pyarrow.parquet.read_table(buf) and select columns)
            yield {"path": item["path"], "size": item["size"]}

# Usage in Lightning
# dataset = CDNParquetIterable("batches/public-merged/2026-05-03/files.json", "axentx/surrogate-1-training-pairs")
# dataloader = torch.utils.data.DataLoader(dataset, batch_size=None)
```

### Studio reuse + safe run (launcher snippet)
```python
from lightning import Studio, Machine, Teamspace

def run_training_launcher():
    ts = Teamspace()
    studio_name = "surrogate-1-train"
    running = None
    for s in ts.studios:
        if s.name == studio_name and s.status == "Running":
            running = s
            break

    if running is None:
        running = Studio.create(
            name=studio_name,
            machine=Machine.L40S,
            create_ok=True
        )
    elif running.status != "Running":
        running.start(machine=Machine.L40S)

    # run your training script via .run()
    # running.run(["python", "train.py", "--manifest", "batches/public-merged/2026-05-03/files.json"])
```

---

## Quick Validation

```bash
chmod +x bin/snapshot.sh
HF_TOKEN=... bin/snapshot.sh 2026-05-03
cat batches/public-merged/2026-05-03/files.json | head
```

Expected: JSON with CDN paths and sizes; no HF API calls during training.
