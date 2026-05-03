# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs, and a training script that uses **CDN-only** fetches (zero HF API calls during training). This directly applies the CDN bypass pattern, avoids HF API rate-limits, and works reliably on Lightning.

---

### Steps (1h 45m total)

1. **Create `tools/snapshot_manifest.py`** (25m)  
   - Single API call: `list_repo_tree(path=date_partition, recursive=True, repo_type="dataset")`  
   - Filter to parquet/jsonl files  
   - Emit `file_manifest.json` with `cdn_url`, `repo_path`, `size`, `date_partition`, `snapshot_ts`  
   - Validate manifest integrity (non-empty, valid URLs)

2. **Add robust CDN fetcher module** (25m)  
   - `surrogate_1/data/cdn_loader.py` with `iter_cdn_files(manifest_path, columns=("prompt","response"), max_retries=3, timeout=30)`  
   - Use `pyarrow.parquet` (for parquet) and `jsonlines` (for jsonl) with streaming  
   - Retry/backoff on CDN failures; skip corrupt files; log warnings; validate columns exist

3. **Update training script to use manifest + CDN-only** (35m)  
   - Accept `--manifest` arg; replace `load_dataset(streaming=True)` with `iter_cdn_files()`  
   - Wrap in `IterableDataset`; keep tokenization/collate identical  
   - Add row-count sanity check (optional sample) and zero-HF-API assertion during data loading

4. **Add Mac/Linux orchestration snippet** (10m)  
   - One-liner to run snapshot then launch Lightning Studio with manifest baked into training args

5. **Smoke test** (20m)  
   - Run snapshot on a small date partition  
   - Run 100 steps of training locally (CPU) to verify data pipeline works  
   - Confirm zero HF API calls during data loading (check logs/network)

---

### Code Snippets

#### tools/snapshot_manifest.py
```python
#!/usr/bin/env python3
"""
Create a CDN-only manifest for a date partition in axentx/surrogate-1-training-pairs.
Usage:
    python tools/snapshot_manifest.py 2026-05-01 --out file_manifest.json
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

from huggingface_hub import HfApi

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

def build_manifest(date_partition: str, out_path: Path) -> Dict[str, Any]:
    api = HfApi()
    # Single API call: list tree for this partition
    tree = api.list_repo_tree(
        repo_id=HF_REPO,
        path=date_partition,
        recursive=True,
        repo_type="dataset",
    )

    files = []
    total_size = 0
    for entry in tree:
        if entry.type != "file":
            continue
        if not (entry.path.endswith(".parquet") or entry.path.endswith(".jsonl")):
            continue
        cdn_url = f"{CDN_ROOT}/{entry.path}"
        files.append({
            "cdn_url": cdn_url,
            "repo_path": entry.path,
            "local_path": entry.path,  # repo-relative path
            "size": entry.size or 0,
        })
        total_size += entry.size or 0

    if not files:
        raise ValueError(f"No parquet/jsonl files found for partition '{date_partition}'")

    manifest = {
        "dataset_repo": HF_REPO,
        "date_partition": date_partition,
        "snapshot_ts": datetime.now(timezone.utc).isoformat(),
        "total_files": len(files),
        "total_bytes": total_size,
        "files": files,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote manifest for {len(files)} files ({total_size / 1e9:.2f} GB) -> {out_path}")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create CDN manifest for a date partition.")
    parser.add_argument("date_partition", help="Partition path, e.g. batches/public-merged/2026-05-01")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    args = parser.parse_args()
    build_manifest(args.date_partition, Path(args.out))
```

#### surrogate_1/data/cdn_loader.py
```python
import json
import jsonlines
import pyarrow.parquet as pq
from typing import Iterator, Dict, Any, Tuple, Optional
from pathlib import Path
import requests
from io import BytesIO
import time

def iter_cdn_files(
    manifest_path: Path,
    columns: Tuple[str, str] = ("prompt", "response"),
    max_files: Optional[int] = None,
    max_retries: int = 3,
    timeout: int = 30,
) -> Iterator[Dict[str, Any]]:
    """
    Stream rows from CDN URLs listed in manifest.
    Yields dicts with at least `prompt` and `response`.
    """
    with open(manifest_path) as f:
        manifest = json.load(f)

    files = manifest["files"]
    if max_files:
        files = files[:max_files]

    prompt_col, response_col = columns
    for idx, entry in enumerate(files):
        url = entry["cdn_url"]
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.get(url, timeout=timeout)
                resp.raise_for_status()
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                wait = 0.5 * (2 ** (attempt - 1))
                print(f"Attempt {attempt}/{max_retries} failed for {url}: {exc}; retrying in {wait:.1f}s")
                time.sleep(wait)

        if last_exc is not None:
            print(f"Failed to fetch {url} after {max_retries} attempts: {last_exc}")
            continue

        data = BytesIO(resp.content)
        try:
            if url.endswith(".parquet"):
                table = pq.read_table(data, columns=[prompt_col, response_col])
                col_names = table.column_names
                if prompt_col not in col_names or response_col not in col_names:
                    print(f"Skipping {url}: missing columns {columns}. Available: {col_names}")
                    continue
                for row in table.to_pylist():
                    if row.get(prompt_col) and row.get(response_col):
                        yield {
                            "prompt": row[prompt_col],
                            "response": row[response_col],
                            "_source_file": entry["repo_path"],
                        }
            elif url.endswith(".jsonl"):
                data.seek(0)
                first = None
                for line in jsonlines.Reader(data):
                    if first is None:
                        first = line
                        if prompt_col not in line or response_col not in line:
                            print(f"Skipping {url}: missing columns {columns}. Sample keys: {list(line.keys())}")
                            break
                    if line.get(prompt_col) and line.get(response_col):
                        yield {
                            "prompt": line[prompt_col],
                            "response": line[response_col],
                            "_source_file": entry["repo_path"],
                        }
                continue
            else:
                print(f"Skipping unsupported file: {url}")
                continue
        except Exception as exc:
            print(f"Failed to parse {url}: {exc}")
            continue

        if (idx + 1) % 10 == 0:
            print(f"Processed {idx + 1}/{len(files)} files")
```

#### Example training script snippet (train.py)
```python
import argparse
from pathlib import Path
from torch.utils.data import IterableDataset, DataLoader

from surrogate_1.data.cdn_loader import iter_cdn_files

class CDNIterableDataset(
