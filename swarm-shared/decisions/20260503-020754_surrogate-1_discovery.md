# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side, one `list_repo_tree` call per date folder) to enumerate files deterministically; embeds the list at build time or fetches once per run.
- Downloads only assigned shard files via **HF CDN bypass** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) — zero API/auth calls during data load, avoids 429 rate limits.
- Projects each file to `{prompt, response}` at parse time (no schema assumptions), computes content hash for dedup, and streams output to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.
- Keeps the existing `lib/dedup.py` SQLite dedup store interface unchanged.
- Adds a small Mac-side helper (`tools/gen-file-list.py`) to produce `file-list.json` for a given date folder (single API call, respects HF rate limits).
- Adds a GitHub Actions step to generate/upload the file list as an artifact or embed it in the repo before the matrix runs (avoids 16 workers calling `list_repo_tree`).

Why this is highest-value (<2h):
- Solves the HF API rate-limit + schema heterogeneity problems in one change.
- Reuses existing dedup logic and output format — no downstream changes.
- Fits within 2 hours: ~1.5h implementation + 0.5h testing/validation.

---

### Files to create/modify

1. `bin/dataset-enrich.py` — new worker (replaces shell script).
2. `tools/gen-file-list.py` — Mac-side helper to create `file-list.json`.
3. `.github/workflows/ingest.yml` — minor tweak to pass matrix vars and optionally upload/download file-list artifact.
4. `requirements.txt` — add `requests` if not present.

---

### Code snippets

#### `tools/gen-file-list.py` (run on Mac)

```python
#!/usr/bin/env python3
"""
Generate file-list.json for a date folder in axentx/surrogate-1-training-pairs.
Usage:
  python tools/gen-file-list.py --date 2026-05-03 --out file-list.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

REPO = "axentx/surrogate-1-training-pairs"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-03")
    parser.add_argument("--out", default="file-list.json")
    args = parser.parse_args()

    api = HfApi()
    # Single non-recursive call per date folder (avoids 100x pagination)
    entries = api.list_repo_tree(repo_id=REPO, path=args.date, recursive=False)
    files = [e.path for e in entries if e.type == "file"]

    payload = {
        "date": args.date,
        "files": sorted(files),
        "repo": REPO,
    }

    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

---

#### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Shard-based CDN-bypass ingestion worker.

Environment:
  SHARD_ID (int): 0..15
  SHARD_TOTAL (int): 16
  HF_TOKEN (str): write token for axentx/surrogate-1-training-pairs
  FILE_LIST (str): path to file-list.json (or embed in repo)
"""
import json
import os
import sys
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, Iterable

try:
    import requests
except ImportError:
    print("ERROR: install requests", file=sys.stderr)
    sys.exit(1)

# Local imports
try:
    from lib.dedup import DedupStore
except ImportError as e:
    print(f"ERROR: cannot import lib.dedup: {e}", file=sys.stderr)
    sys.exit(1)

REPO = "axentx/surrogate-1-training-pairs"
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def shard_filter(items: list, shard_id: int, shard_total: int) -> list:
    """Deterministic 1/N shard by index."""
    return [items[i] for i in range(len(items)) if i % shard_total == shard_id]

def content_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def parse_file_to_pairs(local_path: Path) -> Iterable[Dict[str, Any]]:
    """
    Project file to {prompt, response} at parse time.
    Supports common formats seen in surrogate-1 repos:
      - JSONL with 'prompt'/'response' or 'instruction'/'output'
      - Parquet (via pyarrow) — project only these two columns
    """
    suffix = local_path.suffix.lower()

    if suffix == ".jsonl":
        with open(local_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input")
                response = obj.get("response") or obj.get("output") or obj.get("completion")
                if prompt is None or response is None:
                    continue
                yield {"prompt": str(prompt), "response": str(response)}
        return

    if suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError:
            print("WARNING: pyarrow not installed, skipping parquet", file=sys.stderr)
            return

        tbl = pq.read_table(local_path, columns=["prompt", "response"])
        # If columns missing, try alternate names
        if tbl.num_columns < 2:
            tbl = pq.read_table(local_path)
            col_map = {}
            for c in tbl.column_names:
                low = c.lower()
                if "prompt" in low or "instruction" in low or "input" in low:
                    col_map["prompt"] = c
                if "response" in low or "output" in low or "completion" in low:
                    col_map["response"] = c
            if "prompt" in col_map and "response" in col_map:
                tbl = tbl.select([col_map["prompt"], col_map["response"]])
            else:
                return

        prompts = tbl.column("prompt").to_pylist()
        responses = tbl.column("response").to_pylist()
        for p, r in zip(prompts, responses):
            if p is None or r is None:
                continue
            yield {"prompt": str(p), "response": str(r)}
        return

    # Fallback: ignore unknown formats
    print(f"WARNING: unsupported file {local_path}", file=sys.stderr)

def download_cdn(url: str, dest: Path, timeout: int = 30) -> bool:
    """Download via CDN (no auth)."""
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"ERROR downloading {url}: {e}", file=sys.stderr)
        return False

def main():
    shard_id = int(os.environ.get("SHARD_ID", 0))
    shard_total = int(os.environ.get("SHARD_TOTAL", 16))
    hf_token = os.environ.get("HF_TOKEN", "")
    file_list_path = os.environ.get("FILE_LIST", "file-list.json")

    if not Path(file_list_path).exists():
        print(f"ERROR: file-list not found
