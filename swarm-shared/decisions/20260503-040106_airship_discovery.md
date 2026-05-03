# airship / discovery

## Highest-value incremental improvement (≤2h)
**Goal**: Eliminate HF API rate-limit failures during Surrogate training and make Lightning training resilient to idle timeouts.

- Create `scripts/list_hf_files.py` — one-time Mac-side call to list a date folder and emit `filelist.json` (no recursive pagination).
- Update `surrogate/train.py` to:
  - Load `filelist.json` and stream parquet via HuggingFace CDN URLs (no `load_dataset`, no API calls during training).
  - Project only `{prompt,response}` at parse time (handles mixed schemas safely).
  - Reuse a running Lightning Studio or restart cleanly if idle-killed.
- Add `requirements.txt` additions (`requests`, `pyarrow`, `lightning`, `tqdm`) and a small usage note.

Estimated effort: ~90–120 minutes (including tests).

---

## Implementation plan

1. **Create file-listing script** (`scripts/list_hf_files.py`)
   - Inputs: `repo`, `date_path` (e.g. `batches/mirror-merged/2026-05-03`)
   - Uses `list_repo_tree(..., recursive=False)` once and writes `filelist.json` with CDN-ready URLs.
   - Exits with non-zero on 429 and prints retry-after guidance.

2. **Update training script** (`surrogate/train.py`)
   - Accept `--filelist` (default `filelist.json`) and optional `--repo` for CDN base.
   - Stream parquet files via CDN URLs using `requests` + `pyarrow` (memory-efficient iterator).
   - Map each row to `{prompt, response}`; drop extra columns.
   - Wrap Lightning `Studio.run()` with status check and restart on stopped/idle state.

3. **Add/verify dependencies** (`requirements.txt`)
   - Add `requests`, `pyarrow`, `lightning`, `tqdm` if missing.

4. **Smoke test**
   - Run listing script (once) → verify `filelist.json`.
   - Run training with `--dry-run` to validate loader and Studio lifecycle.

---

## Code snippets

### scripts/list_hf_files.py
```python
#!/usr/bin/env python3
"""
List files in a HuggingFace dataset folder (non-recursive) and emit CDN URLs.
Usage:
    python scripts/list_hf_files.py --repo <dataset_repo> --path <folder> [--out filelist.json]
"""

import argparse
import json
import os
import sys
import time
from typing import List, Dict

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_hf_folder(repo: str, path: str, out_path: str) -> None:
    api = HfApi()
    try:
        items = api.list_repo_tree(repo=repo, path=path, recursive=False)
    except Exception as exc:
        # Heuristic: if 429-like, ask user to wait
        msg = str(exc).lower()
        if "429" in msg or "rate limit" in msg:
            print("ERROR: HF API rate-limited (429). Wait ~360s and retry.", file=sys.stderr)
        sys.exit(1)

    files: List[Dict[str, str]] = []
    for item in items:
        # items may be dict-like or objects; normalize
        rpath = getattr(item, "path", item.get("path", None))
        if not rpath or rpath.endswith("/"):
            continue
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=rpath)
        files.append({"repo": repo, "path": rpath, "cdn_url": cdn_url})

    payload = {
        "repo": repo,
        "folder": path.rstrip("/"),
        "files": files,
        "generated_by": "list_hf_files.py",
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List HF dataset folder and emit CDN filelist.")
    parser.add_argument("--repo", required=True, help="HuggingFace dataset repo (user/repo)")
    parser.add_argument("--path", required=True, help="Folder path inside dataset (e.g. batches/mirror-merged/2026-05-03)")
    parser.add_argument("--out", default="filelist.json", help="Output JSON path")
    args = parser.parse_args()

    list_hf_folder(repo=args.repo, path=args.path, out_path=args.out)
```

### surrogate/train.py
```python
#!/usr/bin/env python3
"""
Surrogate training script (CDN-only parquet loader + Lightning Studio resilience).

Usage:
    python surrogate/train.py --filelist filelist.json [--epochs 1] [--dry-run]
"""

import argparse
import json
import os
import sys
import tempfile
from io import BytesIO
from typing import Iterator, Dict, Any

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# Lightning imports (may raise; install via requirements)
try:
    from lightning import Studio, Machine, Teamspace
except ImportError:
    print("Install: pip install lightning")
    Studio = Machine = Teamspace = None  # type: ignore

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def cdn_parquet_rows(filelist_path: str, repo: str | None = None) -> Iterator[Dict[str, Any]]:
    """Stream rows from parquet files listed in filelist.json using CDN URLs."""
    with open(filelist_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    files = manifest.get("files", [])
    if not files:
        raise ValueError("No files found in filelist.")

    # If repo provided, override CDN base; otherwise use manifest or URL in file entries.
    for entry in tqdm(files, desc="Streaming parquet"):
        if isinstance(entry, dict):
            r = entry.get("repo", repo)
            p = entry["path"]
        else:
            # fallback
            r = repo
            p = str(entry)

        if not r:
            raise ValueError("repo must be provided or present in filelist entries.")

        url = CDN_TEMPLATE.format(repo=r, path=p)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()

        # Read parquet from memory
        table = pq.read_table(BytesIO(resp.content))
        df = table.to_pandas()

        # Project only prompt/response; tolerate missing columns
        for _, row in df.iterrows():
            prompt = row.get("prompt", row.get("input", row.get("text", "")))
            response = row.get("response", row.get("output", ""))
            if prompt or response:
                yield {"prompt": str(prompt), "response": str(response)}

def run_training(filelist_path: str, repo: str | None, dry_run: bool = False, epochs: int = 1) -> None:
    if dry_run:
        print("Dry-run: validating loader...")
        count = 0
        for item in cdn_parquet_rows(filelist_path, repo=repo):
            count += 1
            if count <= 3:
                print(item)
        print(f"Dry-run complete. {count} rows projected.")
        return

    # Lightweight training stub: replace with real model/train loop.
    print("Starting training (CDN-only loader)...")
    total = 0
    for item in cdn_parquet_rows(filelist_path, repo=repo):
        total += 1
        # TODO: feed item['prompt'], item['response'] into model
        if total % 1000 == 0:
            print(f"Processed {total} rows")
    print(f"Training finished (processed {total} rows).")

def run_lightning_studio(script_args: list[str], studio_name: str = "surrogate-train") -> None:
    """
    Reuse a running Lightning Studio or restart if stopped/idle-killed.
    """
    if Studio is None:
        print("Lightning not available; skipping Studio launch.")
        return
