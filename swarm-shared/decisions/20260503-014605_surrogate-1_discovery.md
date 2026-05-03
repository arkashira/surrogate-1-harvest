# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** that:

- Uses a **single `list_repo_tree` snapshot** (JSON manifest) generated once per date on the Mac orchestrator and committed to the repo (or passed via workflow artifact).
- Each GitHub Actions shard reads the manifest, filters by deterministic hash-bucket (`SHARD_ID`), downloads only its slice via **HF CDN** (`resolve/main/...`) with zero API calls during stream.
- Projects heterogeneous files to `{prompt, response}` at parse time (no `load_dataset(streaming=True)` on mixed schemas).
- Deduplicates via central `lib/dedup.py` md5 store and writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Commits use date/slug partitioning to stay under HF commit cap; retries respect 429/360s backoff.

### Steps (≤2h)

1. **Create manifest generator** (`bin/gen-manifest.py`) — run on Mac orchestrator once per date.
2. **Rewrite `bin/dataset-enrich.sh`** to be manifest-driven + CDN-only fetches.
3. **Add lightweight Python helper** (`bin/worker.py`) for robust CDN download + schema projection.
4. **Update workflow** to pass `MANIFEST_PATH` and `DATE` (or commit pre-generated manifest).
5. **Smoke test** one shard locally.

---

## File: `bin/gen-manifest.py`

```python
#!/usr/bin/env python3
"""
Generate a date-scoped manifest for surrogate-1 ingestion.
Usage (Mac orchestrator):
  HF_TOKEN=... python bin/gen-manifest.py \
    --repo axentx/some-public-raw \
    --date 2026-05-03 \
    --out manifests/2026-05-03.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi, login

def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--out", required=True)
    p.add_argument("--token", default=os.getenv("HF_TOKEN"))
    return p

def main():
    args = build_parser().parse_args()
    if not args.token:
        print("HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    login(token=args.token)
    api = HfApi()

    # List top-level date folder only (avoid recursive on big repos)
    prefix = f"raw/{args.date}/"
    entries = api.list_repo_tree(repo_id=args.repo, path=prefix, recursive=False)

    files = []
    for e in entries:
        if not e.path.endswith((".json", ".jsonl", ".parquet", ".csv")):
            continue
        files.append({
            "path": e.path,
            "cdn_url": f"https://huggingface.co/datasets/{args.repo}/resolve/main/{e.path}"
        })

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": sorted(files, key=lambda x: x["path"])
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

## File: `bin/worker.py`

```python
#!/usr/bin/env python3
"""
CDN-only worker shard for surrogate-1 ingestion.
Deterministic shard assignment by file path hash.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from datasets import Features, Sequence, Value

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore

CDN_TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_429 = 360

def shard_for(path: str, n_shards: int) -> int:
    h = int(hashlib.sha256(path.encode()).hexdigest(), 16)
    return h % n_shards

def robust_get(url: str):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, timeout=CDN_TIMEOUT, stream=False)
            if r.status_code == 429:
                print(f"429 rate-limited, waiting {BACKOFF_429}s", file=sys.stderr)
                import time; time.sleep(BACKOFF_429)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            import time; time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")

def project_to_pair(obj, path: str):
    """
    Best-effort projection to {prompt, response}.
    Extend per known schema as needed.
    """
    # Common patterns
    if isinstance(obj, dict):
        # HF conversational datasets
        if "messages" in obj and isinstance(obj["messages"], list):
            texts = [m.get("content", "") for m in obj["messages"] if isinstance(m, dict)]
            if len(texts) >= 2:
                return {"prompt": texts[-2], "response": texts[-1]}
        # Simple prompt/response
        if "prompt" in obj and "response" in obj:
            return {"prompt": str(obj["prompt"]), "response": str(obj["response"])}
        # Completion style
        if "instruction" in obj and "output" in obj:
            return {"prompt": str(obj["instruction"]), "response": str(obj["output"])}
        # Last-ditch: first two string fields
        strs = [v for v in obj.values() if isinstance(v, str) and v.strip()]
        if len(strs) >= 2:
            return {"prompt": strs[0], "response": strs[1]}
    return None

def process_parquet(url: str):
    r = robust_get(url)
    with open("/tmp/temp.parquet", "wb") as f:
        f.write(r.content)
    try:
        table = pq.read_table("/tmp/temp.parquet")
        for batch in table.to_batches(max_chunksize=1000):
            df = batch.to_pandas()
            for _, row in df.iterrows():
                pair = project_to_pair(row.to_dict(), url)
                if pair:
                    yield pair
    finally:
        if os.path.exists("/tmp/temp.parquet"):
            os.unlink("/tmp/temp.parquet")

def process_jsonlines(url: str):
    r = robust_get(url)
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        pair = project_to_pair(obj, url)
        if pair:
            yield pair

def process_json(url: str):
    r = robust_get(url)
    try:
        data = r.json()
    except Exception:
        return
    if isinstance(data, list):
        for obj in data:
            pair = project_to_pair(obj, url)
            if pair:
                yield pair
    else:
        pair = project_to_pair(data, url)
        if pair:
            yield pair

def process_csv(url: str):
    r = robust_get(url)
    from io import StringIO
    import csv
    stream = StringIO(r.text)
    reader = csv.DictReader(stream)
    for row in reader:
        pair = project_to_pair(row, url)
        if pair:
            yield pair

def process_file(entry):
    url = entry["cdn_url"]
    path = entry["path"]
    if path.endswith(".parquet"):
        yield from process_parquet(url)
    elif path.endswith(".jsonl"):
        yield from process_jsonlines(url)
    elif path.endswith(".json"):
        yield from process_json(url)
    elif path.endswith
