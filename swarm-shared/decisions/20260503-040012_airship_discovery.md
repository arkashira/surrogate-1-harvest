# airship / discovery

## Final Implementation Plan (≤2 h)

**Highest-value change**: Add a CDN-only parquet loader and Lightning idle-resilient runner to `/opt/axentx/airship/surrogate/train.py` plus `scripts/list_hf_files.py`. This eliminates Hugging Face API calls during training, bypasses rate limits, and keeps training alive across Lightning idle stops.

### Steps (120 min total)

1. **Create `scripts/list_hf_files.py`** (20 min)  
   - Single API call to `list_repo_tree(path, recursive=False)` for a date folder.  
   - Save `{"repo": "...", "date": "...", "files": [...]}` to JSON.  
   - Embeddable by train.py.

2. **Create/update `/opt/axentx/airship/surrogate/train.py`** (60 min)  
   - CDN-only parquet loader: `requests.get("https://huggingface.co/datasets/{repo}/resolve/main/{path}")` with streaming + `pyarrow.parquet.ParquetFile` on bytes.  
   - Project to `{prompt, response}` at parse time (avoid mixed-schema loads).  
   - Lightning idle-resilient runner: check studio status before each run; if stopped, restart with `target.start(machine=Machine.L40S)` (or fallback to available machine).  
   - Reuse running studio if found.

3. **Add entrypoint script `scripts/run_training.sh`** (10 min)  
   - Bash shebang, executable, sets `SHELL=/bin/bash`.  
   - Invokes `python surrogate/train.py --file-list ...`.

4. **Smoke test** (30 min)  
   - Run list script → verify JSON.  
   - Run train.py in dry-run mode (1 batch) → verify CDN fetch + Lightning reconnect.

---

## `scripts/list_hf_files.py`

```python
#!/usr/bin/env python3
"""
List parquet files for a date folder in a HuggingFace dataset repo.

Usage:
    python scripts/list_hf_files.py \
        --repo axentx/surrogate-data \
        --date 2026-04-29 \
        --out scripts/file_list.json
"""
import argparse
import json
import os
import sys
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

def main() -> None:
    parser = argparse.ArgumentParser(description="List parquet files in a dataset repo folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. axentx/surrogate-data)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    parser.add_argument("--out", default="file_list.json", help="Output JSON path")
    args = parser.parse_args()

    api = HfApi()
    folder = f"batches/mirror-merged/{args.date}"
    try:
        items = api.list_repo_tree(repo_id=args.repo, path=folder, recursive=False)
    except Exception as e:
        print(f"Error listing repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    files = sorted(
        f"{folder}/{item.path.split('/')[-1]}"
        for item in items
        if item.path.lower().endswith(".parquet")
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "repo": args.repo,
        "date": args.date,
        "folder": folder,
        "files": files,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

---

## `/opt/axentx/airship/surrogate/train.py`

```python
#!/usr/bin/env python3
"""
CDN-only parquet loader + Lightning idle-resilient surrogate training runner.

Usage:
    python surrogate/train.py --file-list scripts/file_list.json --dry-run
"""
import argparse
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import pyarrow.parquet as pq
import requests

# Optional Lightning imports (install if needed)
try:
    from lightning import Fabric, LightningFlow, LightningWork, Machine, Teamspace
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def load_parquet_from_cdn(repo: str, path: str, columns: Optional[List[str]] = None):
    """Download parquet via CDN and return a pyarrow Table with selected columns."""
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    b = io.BytesIO(resp.content)
    pf = pq.ParquetFile(b)
    # Project only needed columns to avoid mixed-schema issues
    if columns is None:
        available = pf.schema.names
        prompt_col = next((c for c in ("prompt", "instruction", "input") if c in available), None)
        response_col = next((c for c in ("response", "output") if c in available), None)
        columns = [c for c in [prompt_col, response_col] if c]
        if not columns:
            columns = available[:2]
    return pf.read(columns=columns)

def build_dataset(file_list_path: str):
    """Yield {prompt, response} rows from CDN parquet files."""
    data = json.loads(Path(file_list_path).read_text())
    repo = data["repo"]
    for path in data["files"]:
        tbl = load_parquet_from_cdn(repo, path, columns=["prompt", "response"])
        df = tbl.to_pandas()
        for _, row in df.iterrows():
            prompt = row.get("prompt") or row.get("instruction") or ""
            response = row.get("response") or row.get("output") or ""
            if prompt and response:
                yield {"prompt": str(prompt), "response": str(response)}

# ---- Lightning runner (optional) ----

class SurrogateTrainer(LightningFlow):
    def __init__(self, file_list_path: str, max_steps: int = 1000):
        super().__init__()
        self.file_list_path = file_list_path
        self.max_steps = max_steps
        self.studio_name = "surrogate-train-studio"
        self._studio = None

    def _get_or_create_studio(self):
        ts = Teamspace()
        for s in ts.studios:
            if s.name == self.studio_name and s.status == "Running":
                print(f"Reusing running studio: {self.studio_name}")
                return s
        print(f"Creating studio: {self.studio_name}")
        # Prefer L40S on free/public cloud; fallback to available
        return ts.studios.create(
            self.studio_name,
            machine=Machine.L40S,
            shutdown_delay_minutes=30,
            create_ok=True,
        )

    def run_training(self):
        if not LIGHTNING_AVAILABLE:
            print("Lightning not available; running local epoch simulation.")
            count = 0
            for item in build_dataset(self.file_list_path):
                # Replace with real training step
                count += 1
                if count >= self.max_steps:
                    break
            print(f"Local run finished ({count} steps).")
            return

        studio = self._get_or_create_studio()
        self._studio = studio

        # Idle-resilient run: if studio stopped, restart and re-run
        target = studio.run(
            "python surrogate/train.py --file-list {file_list} --max-steps {max_steps}".format(
                file_list=self.file_list_path,
                max_steps=self.max_steps,
            ),
            wait=False,
        )

        while True:
            time.sleep(30)
            studio.refresh()
            if studio.status == "Stopped":
                print("Studio stopped (idle timeout). Restarting...")
                studio.start(machine=Machine.L40S)
                # Re-launch the run after restart
                target =
