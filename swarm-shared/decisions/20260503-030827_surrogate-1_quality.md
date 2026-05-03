# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Single Mac-side `list_repo_tree` call (once per date) → save `manifest-{DATE}.json`
- Worker uses **HF CDN URLs** (`resolve/main/...`) to bypass API rate limits during streaming
- Projects heterogeneous files to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Deterministic shard assignment via `hash(slug) % SHARD_TOTAL`
- Central dedup via existing `lib/dedup.py` (SQLite md5 store)
- Outputs `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with no extra metadata columns
- Reuses existing GitHub Actions matrix (no workflow changes)

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Environment:
  SHARD_ID      (int) 0..15
  SHARD_TOTAL   (int) default 16
  DATE          (str) YYYY-MM-DD
  HF_TOKEN      (str) write token for axentx/surrogate-1-training-pairs
  FILE_LIST     (str) optional path to manifest-{DATE}.json
  REPO          (str) default "axentx/surrogate-1-training-pairs"
"""
import os
import sys
import json
import hashlib
import datetime
from pathlib import Path

import requests
from huggingface_hub import HfApi, hf_hub_download

# ── config --
REPO = os.getenv("REPO", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE = os.getenv("DATE", datetime.date.today().isoformat())
HF_TOKEN = os.getenv("HF_TOKEN")
FILE_LIST = os.getenv("FILE_LIST")  # optional pre-generated manifest
OUT_DIR = Path(f"batches/public-merged/{DATE}")
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

# ── dedup --
from lib.dedup import is_duplicate, add_hash  # existing SQLite store

# ── helpers --
def list_files_via_tree() -> list[str]:
    """Single API call: list date folder contents (non-recursive)."""
    api = HfApi(token=HF_TOKEN)
    items = api.list_repo_tree(
        repo_id=REPO,
        path=DATE,
        repo_type="dataset",
        recursive=False,
    )
    files = []
    for it in items:
        if it.type == "file":
            files.append(it.path)
        else:
            sub = api.list_repo_tree(
                repo_id=REPO,
                path=it.path,
                repo_type="dataset",
                recursive=False,
            )
            for s in sub:
                if s.type == "file":
                    files.append(s.path)
    return files

def load_file_list() -> list[str]:
    if FILE_LIST and Path(FILE_LIST).exists():
        with open(FILE_LIST) as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    return list_files_via_tree()

def belongs_to_shard(path: str) -> bool:
    slug = path.rsplit(".", 1)[0].replace("/", "-")
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return (h % SHARD_TOTAL) == SHARD_ID

def cdn_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"

# ── schema projectors --
def try_jsonl_lines(content: bytes):
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue

def project_to_pair(obj) -> dict | None:
    """Return {prompt, response} or None."""
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
    response = obj.get("response") or obj.get("output") or obj.get("answer")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def extract_pairs_from_file(path: str):
    url = cdn_url(path)
    resp = requests.get(url, timeout=30)
    if resp.status_code != 200:
        print(f"CDN fetch failed {path}: {resp.status_code}", file=sys.stderr)
        return
    content = resp.content
    suffix = Path(path).suffix.lower()

    if suffix == ".jsonl":
        for obj in try_jsonl_lines(content):
            pair = project_to_pair(obj)
            if pair:
                yield pair
    elif suffix == ".json":
        try:
            data = json.loads(content)
        except Exception:
            return
        if isinstance(data, list):
            for obj in data:
                pair = project_to_pair(obj)
                if pair:
                    yield pair
        else:
            pair = project_to_pair(data)
            if pair:
                yield pair
    elif suffix in (".parquet", ".arrow"):
        local_path = hf_hub_download(
            repo_id=REPO,
            filename=path,
            repo_type="dataset",
            token=HF_TOKEN,
        )
        try:
            import pyarrow.parquet as pq
            tbl = pq.read_table(local_path, columns=["prompt", "response"])
            for batch in tbl.to_batches(max_chunksize=1000):
                df = batch.to_pandas()
                for _, row in df.iterrows():
                    prompt = row.get("prompt")
                    response = row.get("response")
                    if prompt is not None and response is not None:
                        # basic NaN/None check without pandas dependency
                        if prompt == prompt and response == response:  # NaN != NaN
                            yield {"prompt": str(prompt), "response": str(response)}
        except Exception as e:
            print(f"Parquet read failed {path}: {e}", file=sys.stderr)
        finally:
            if "local_path" in locals() and Path(local_path).exists():
                Path(local_path).unlink(missing_ok=True)
    else:
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            pair = project_to_pair(obj)
            if pair:
                yield pair

# ── main --
def main():
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} | Date {DATE}")
    files = load_file_list()
    print(f"Total files in date folder: {len(files)}")

    my_files = [f for f in files if belongs_to_shard(f)]
    print(f"Shard files: {len(my_files)}")

    written = 0
    skipped_dup = 0
    with OUT_FILE.open("w", buffering=1 << 20) as out:
        for path in my_files:
            for pair in extract_pairs_from_file(path):
                line = json.dumps(pair, ensure_ascii=False)
                h = hashlib.md5(line.encode()).hexdigest()
                if is_duplicate(h):
                    skipped_dup += 1
                    continue
                add_hash(h)
                out.write(line + "\n")
                written += 1

    print(f"Done: written={written}, skipped_dup={skipped_dup}")

if __name__ == "__main__":
    main()
```
