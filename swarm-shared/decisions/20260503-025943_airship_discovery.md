# airship / discovery

## Highest-Value Incremental Improvement (<2h)

**Goal**: Eliminate HF API 429s during Surrogate training by switching to CDN-only data loading + Lightning idle-resilience, and reduce Mac→Lightning iteration to <2 minutes.

**Why this first**:  
- Directly unblocks surrogate training (the most expensive path).  
- Uses the CDN bypass insight (no auth = no rate limit).  
- Reuses running Lightning Studio (saves quota, avoids H200/L40S free-tier trap).  
- Zero breaking changes to Arkship.

---

## Implementation Plan

### 1) Pre-list file paths once (Mac orchestration)
- Single `list_repo_tree` call for a date folder → save `file_list.json`.
- Embed `file_list.json` in training script so Lightning workers do **only CDN fetches** (`https://huggingface.co/datasets/.../resolve/main/...`).

### 2) CDN-only data loader
- Replace `load_dataset(streaming=True)` with custom iterable that:
  - Iterates `file_list.json`.
  - Downloads each file via `requests.get(cdn_url, timeout=30)`.
  - Projects to `{prompt, response}` at parse time (avoids pyarrow CastError from mixed schemas).
- Add retry + exponential backoff (CDN limits are high; 429 unlikely but handle gracefully).

### 3) Lightning Studio reuse + idle resilience
- Before `.run()`, list `Teamspace.studios`; reuse a running studio with matching name.
- If studio is stopped, restart with `target.start(machine=Machine.L40S)` (or fallback to free-tier size).
- Wrap training entrypoint so it can be re-run safely without recreating the studio.

### 4) Quick validation script
- Small CLI on Mac: `python run_surrogate_train.py --date-folder 2026-05-03 --dry-run` to verify file list and one CDN fetch.

---

## Code Snippets

### File: `scripts/build_file_list.py` (run on Mac)
```python
#!/usr/bin/env python3
"""
Generate file_list.json for a date folder to enable CDN-only training.
Run on Mac (or any dev machine) before training.
"""
import json
import os
import sys
from huggingface_hub import HfApi

API_TOKEN = os.getenv("HF_TOKEN", "")  # optional; public datasets don't require it for CDN
REPO_ID = "axentx/surrogate-dataset-mirror"
DATE_FOLDER = sys.argv[1] if len(sys.argv) > 1 else "2026-05-03"
OUTPUT = "file_list.json"

def main():
    api = HfApi(token=API_TOKEN)
    # Non-recursive per-folder listing to minimize API usage
    entries = api.list_repo_tree(repo_id=REPO_ID, path=DATE_FOLDER, recursive=False)
    files = [e.path for e in entries if e.type == "file"]
    payload = {
        "repo_id": REPO_ID,
        "date_folder": DATE_FOLDER,
        "files": files,
        "cdn_prefix": f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"
    }
    with open(OUTPUT, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} files to {OUTPUT}")

if __name__ == "__main__":
    main()
```

### File: `surrogate/data/cdn_loader.py`
```python
import json
import requests
from typing import Iterator, Dict, Any
from pathlib import Path

class CDNParquetLoader:
    """
    CDN-only loader for surrogate training.
    Projects each file to {prompt, response} at parse time.
    """
    def __init__(self, file_list_path: str):
        with open(file_list_path) as f:
            self.manifest = json.load(f)
        self.prefix = self.manifest["cdn_prefix"].rstrip("/")
        self.files = self.manifest["files"]

    def _stream_file(self, rel_path: str) -> bytes:
        url = f"{self.prefix}/{rel_path}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content

    def _parse_parquet_to_pairs(self, data: bytes) -> Iterator[Dict[str, Any]]:
        import pyarrow.parquet as pq
        import io
        table = pq.read_table(io.BytesIO(data))
        # Keep only prompt/response; ignore extra/mixed columns
        cols = table.column_names
        prompt_col = next((c for c in ["prompt", "instruction", "input"] if c in cols), None)
        response_col = next((c for c in ["response", "output", "completion"] if c in cols), None)
        if prompt_col is None or response_col is None:
            # Skip malformed files; training will filter them out
            return
        df = table.to_pandas()
        for _, row in df.iterrows():
            yield {"prompt": str(row[prompt_col]), "response": str(row[response_col])}

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for rel_path in self.files:
            try:
                data = self._stream_file(rel_path)
                yield from self._parse_parquet_to_pairs(data)
            except Exception as exc:
                # Log and continue; don't fail entire epoch on one bad file
                print(f"Skipping {rel_path}: {exc}")
                continue
```

### File: `surrogate/train.py` (excerpt)
```python
import lightning as L
from pathlib import Path
from surrogate.data.cdn_loader import CDNParquetLoader

def train_step(batch):
    # placeholder for actual surrogate training logic
    return {"loss": 0.0}

def run_training(file_list_path: str):
    dataset = CDNParquetLoader(file_list_path)
    # Example: use with Lightning DataLoader or simple loop
    for i, item in enumerate(dataset):
        if i >= 100:  # quick demo limit
            break
        train_step(item)
    print("Training step completed (CDN-only).")

if __name__ == "__main__":
    # Expect file_list.json next to this script or passed via arg
    fl = Path("file_list.json")
    if not fl.exists():
        raise FileNotFoundError("Run scripts/build_file_list.py first.")
    run_training(str(fl))
```

### File: `scripts/launch_surrogate_studio.py` (Mac orchestration)
```python
#!/usr/bin/env python3
"""
Reuse or start a Surrogate Lightning Studio (L40S preferred).
Keeps iteration fast (<2m) by avoiding studio recreation.
"""
import lightning as L

def get_or_create_studio(name: str = "surrogate-train"):
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {s.name}")
            return s
    # Start new if none running
    print(f"Starting studio: {name}")
    target = L.Studio(
        name=name,
        machine=L.Machine.L40S,
        create_ok=True,
    )
    return target

def main():
    studio = get_or_create_studio()
    # If stopped, restart before run
    if studio.status != "running":
        print("Studio stopped; restarting...")
        studio.start(machine=L.Machine.L40S)
    # Run training (lightweight entrypoint)
    studio.run(
        "python ../train.py",
        wait=False,
    )
    print("Studio run submitted. Monitor in Lightning Studio UI.")

if __name__ == "__main__":
    main()
```

---

## Execution Steps (Mac)

```bash
# 1) Generate file list (once per date folder)
python scripts/build_file_list.py 2026-05-03

# 2) Quick local validation (optional)
python -c "from surrogate.data.cdn_loader import CDNParquetLoader; it = iter(CDNParquetLoader('file_list.json')); print(next(it))"

# 3) Launch/resume Lightning Studio and submit training
python scripts/launch_surrogate_studio.py
```

Expected outcome:  
- HF API called only once (during `build_file_list.py`).  
- Training uses CDN-only downloads → no 429s.  
- Studio reused if running → saves
