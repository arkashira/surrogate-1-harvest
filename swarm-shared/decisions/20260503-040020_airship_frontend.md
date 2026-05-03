# airship / frontend

## Final consolidated implementation (highest-value, correct + actionable)

**Goal**: Eliminate HF API rate limits and Lightning idle timeouts during training by:
- Listing dataset files once (on your Mac) into `file_list.json`
- Training from CDN parquet URLs only
- Reusing or restarting a Lightning Studio automatically so training never blocks on rate limits or idle stops

---

### 1) Create `/opt/axentx/airship/scripts/list_hf_files.py`

Run once on your Mac (after rate-limit window) to snapshot a date folder.

```python
#!/usr/bin/env python3
"""
List parquet files in a single date folder of a Hugging Face dataset repo.
Avoids recursive pagination. Emits file_list.json for training.

Usage:
  python scripts/list_hf_files.py \
    --repo_id datasets/mirror-merged \
    --date 2026-05-01 \
    --out file_list.json
"""

import argparse
import json
import os
import sys

from huggingface_hub import HfApi

HF_TOKEN = os.getenv("HF_TOKEN")  # required for private repos; optional for public

def main() -> None:
    parser = argparse.ArgumentParser(description="List HF dataset parquet files in a date folder (non-recursive).")
    parser.add_argument("--repo_id", required=True, help="HF dataset repo id, e.g. datasets/mirror-merged")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-01")
    parser.add_argument("--out", default="file_list.json", help="Output JSON path")
    parser.add_argument("--prefix", default="batches/mirror-merged", help="Base prefix inside repo")
    args = parser.parse_args()

    api = HfApi(token=HF_TOKEN)
    folder_path = f"{args.prefix}/{args.date}"
    try:
        items = api.list_repo_tree(repo_id=args.repo_id, path=folder_path, recursive=False)
    except Exception as exc:
        print(f"Error listing repo tree: {exc}", file=sys.stderr)
        sys.exit(1)

    parquet_files = sorted(
        item.rfilename for item in items if isinstance(item.rfilename, str) and item.rfilename.lower().endswith(".parquet")
    )

    if not parquet_files:
        print(f"No parquet files found in {folder_path}", file=sys.stderr)

    payload = {"repo_id": args.repo_id, "paths": parquet_files}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(parquet_files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

### 2) Create/replace `/opt/axentx/airship/surrogate/train.py`

CDN-only loader + Lightning idle-resilient runner. Compatible with Lightning Studio free-tier machines (L40S) and graceful fallbacks.

```python
#!/usr/bin/env python3
"""
CDN-only parquet loader + Lightning idle-resilient runner.

- Loads parquet files directly via CDN (no HF API during training).
- Projects only {prompt, response} to avoid mixed-schema errors.
- Reuses running studio; restarts if idle-stopped before each run.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, Iterator, List

import pyarrow as pa
import pyarrow.parquet as pq
import requests
import tensorflow as tf

try:
    from lightning import Machine, Studio, Teamspace  # type: ignore
    _LIGHTNING_AVAILABLE = True
except Exception:
    _LIGHTNING_AVAILABLE = False

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"

# ---------------------------------------------------------------------------
# CDN parquet streaming
# ---------------------------------------------------------------------------
def cdn_parquet_records(repo_id: str, paths: List[str]) -> Iterator[Dict[str, str]]:
    """
    Stream rows from parquet files via CDN.
    Yields dicts with at least {prompt, response} when present.
    """
    for path in paths:
        url = CDN_TEMPLATE.format(repo_id=repo_id, path=path)
        try:
            resp = requests.get(url, timeout=120)
            resp.raise_for_status()
        except Exception as exc:
            print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
            continue

        try:
            table = pq.read_table(pq.ParquetFile(pa.BufferReader(resp.content)))
        except Exception as exc:
            print(f"Failed to read parquet from {url}: {exc}", file=sys.stderr)
            continue

        # Best-effort projection to avoid schema mismatches
        available = [c for c in ("prompt", "response") if c in table.column_names]
        if not available:
            # If expected columns missing, skip file but do not crash
            print(f"No prompt/response columns in {path}; skipping", file=sys.stderr)
            continue

        df = table.select(available).to_pandas()
        for _, row in df.iterrows():
            out: Dict[str, str] = {k: str(row[k]) for k in available}
            yield out

def build_tf_dataset(repo_id: str, paths: List[str], batch_size: int = 8) -> tf.data.Dataset:
    """
    Build a tf.data.Dataset from CDN parquet files.
    Each element is a dict of tensors: {"prompt": <str>, "response": <str>}
    """
    def gen() -> Iterator[Dict[str, tf.Tensor]]:
        for record in cdn_parquet_records(repo_id, paths):
            yield {k: tf.constant(v, dtype=tf.string) for k, v in record.items()}

    # Infer output signature from first valid record (or fallback)
    sample = next(cdn_parquet_records(repo_id, paths[:1]), None)
    if sample is None:
        raise ValueError("No valid records found in provided file list.")

    output_signature = {
        k: tf.TensorSpec(shape=(), dtype=tf.string) for k in sample.keys()
    }

    ds = tf.data.Dataset.from_generator(
        lambda: (r for r in gen()),  # generator must return same structure
        output_signature=output_signature,
    )
    ds = ds.shuffle(1024, reshuffle_each_iteration=True).batch(batch_size).prefetch(tf.data.AUTOTUNE)
    return ds

# ---------------------------------------------------------------------------
# Lightning Studio helpers
# ---------------------------------------------------------------------------
def get_or_create_studio(name: str = "surrogate-train", machine: Machine = Machine.L40S) -> Studio:
    """
    Reuse a running studio; if stopped or missing, (re)create/start.
    """
    if not _LIGHTNING_AVAILABLE:
        raise RuntimeError("lightning-sdk not available; install lightning to use Studio features")

    ts = Teamspace()
    running = [s for s in ts.studios if s.name == name and getattr(s, "status", None) == "Running"]
    if running:
        print(f"Reusing running studio: {name}")
        return running[0]

    stopped = [s for s in ts.studios if s.name == name and getattr(s, "status", None) == "Stopped"]
    if stopped:
        s = stopped[0]
        print(f"Starting stopped studio: {name}")
        s.start(machine=machine)
        return s

    print(f"Creating studio: {name}")
    return Studio(name=name, machine=machine, create_ok=True)

# ---------------------------------------------------------------------------
# Training entrypoint
# ---------------------------------------------------------------------------
def run_training(file_list_path: str, epochs: int = 1, dry_run: bool = False) -> None:
    with open(file_list_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    repo_id = payload["repo_id"]
    paths = payload["paths"]
    if not paths:
        print("No parquet files in file list; nothing to train.", file=sys.stderr)
        return

    print(f"Loaded {len(paths)} parquet files for repo {repo_id}")

    if dry_run:
        print("Dry-run: validating CDN fetch and parsing for first file...")
        for i, rec in enumerate(cdn_parquet_records(repo_id, paths[:1])):
            if i >= 3:
                break
            print("Sample:", rec)

