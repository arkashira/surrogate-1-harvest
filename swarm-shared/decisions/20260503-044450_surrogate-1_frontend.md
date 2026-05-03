# surrogate-1 / frontend

Below is the **single, merged implementation** that keeps the strongest, most actionable parts of both proposals while resolving contradictions in favor of correctness and deployability.

---

## Final Implementation Plan (≤2 h)

**Highest-value improvement**  
Replace fragile shell-based ingestion with a **manifest-driven, CDN-only Python pipeline** that:

- Eliminates HF API rate limits during training data loads.  
- Prevents mixed-schema `CastError`s by deterministic projection.  
- Guarantees shard-level deduplication across runs.

---

### 1) Add `bin/manifest.py` (single source of truth)

- One `list_repo_tree` call (after rate-limit window) for today’s folder.  
- Produces `manifests/latest.json` with `sha256` and file list.  
- Uploaded as a GitHub Actions artifact so **all workers use identical CDN URLs** (zero HF API calls during streaming).

```python
#!/usr/bin/env python3
"""
Generate a lightweight manifest for today's folder.
Run once per workflow.
"""
import json, os, hashlib, datetime, sys
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/axentx/surrogate-1-training-pairs")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "manifests")
os.makedirs(OUT_DIR, exist_ok=True)

def main() -> None:
    api = HfApi(token=os.getenv("HF_TOKEN"))
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")

    # Non-recursive to avoid pagination explosion; workers recurse only assigned shards
    tree = api.list_repo_tree(repo_id=HF_REPO, path=today, recursive=False)
    files = sorted(entry.path for entry in tree if entry.type == "file")

    manifest = {
        "date": today,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "repo": HF_REPO,
        "files": files,
    }
    manifest["sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()

    out_path = os.path.join(OUT_DIR, "latest.json")
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written: {out_path} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

---

### 2) Add `bin/worker.py` (CDN-only, schema-resilient)

- **Deterministic shard assignment**: `hash(slug) % 16 == SHARD_ID`.  
- **CDN-only fetches** via `resolve/main/...` (no auth, no rate limits).  
- **Schema-safe projection** to `{prompt, response}` with multiple fallbacks.  
- **Central dedup** via `lib/dedup.py` (SQLite) to avoid duplicates across shards/runs.  
- Outputs `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

```python
#!/usr/bin/env python3
"""
CDN-bypass worker for a single shard.
Usage:
  SHARD_ID=3 python bin/worker.py manifests/latest.json
"""
import json, os, sys, hashlib, datetime, io
from pathlib import Path

import pyarrow.parquet as pq
import pyarrow as pa
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", 0))
assert 0 <= SHARD_ID <= 15, "SHARD_ID must be 0-15"

def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def belongs_to_shard(slug: str) -> bool:
    return slug_hash(slug) % 16 == SHARD_ID

def cdn_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{path}"

def extract_pair(record) -> dict | None:
    """
    Best-effort projection to {prompt, response}.
    Handles common schema variants seen in surrogate-1.
    """
    if isinstance(record, dict):
        prompt = record.get("prompt") or record.get("input") or record.get("question")
        response = record.get("response") or record.get("output") or record.get("answer")
        if prompt is not None and response is not None:
            return {"prompt": str(prompt), "response": str(response)}
    return None

def process_file(path: str, dedup: DedupStore, out_f) -> int:
    url = cdn_url(HF_REPO, path)
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
        return 0

    written = 0
    try:
        # Try direct projection first (fastest, safest)
        table = pq.read_table(pa.BufferReader(resp.content), columns=["prompt", "response"])
    except Exception:
        try:
            table = pq.read_table(pa.BufferReader(resp.content))
        except Exception as exc:
            print(f"Cannot read parquet {path}: {exc}", file=sys.stderr)
            return 0

    for batch in table.to_batches():
        cols = batch.columns
        prompt_arr = None
        response_arr = None

        # Resolve columns flexibly
        for name in ("prompt", "input", "question"):
            if name in batch.schema.names:
                prompt_arr = batch.column(name)
                break
        for name in ("response", "output", "answer"):
            if name in batch.schema.names:
                response_arr = batch.column(name)
                break

        if prompt_arr is None or response_arr is None:
            # Fallback to row-wise dict extraction
            rows = batch.to_pylist()
            for row in rows:
                pair = extract_pair(row)
                if pair is None:
                    continue
                fingerprint = dedup.fingerprint(pair["prompt"], pair["response"])
                if dedup.seen(fingerprint):
                    continue
                dedup.add(fingerprint)
                out_f.write(json.dumps(pair) + "\n")
                written += 1
            continue

        # Vectorized path when columns are present
        for i in range(batch.num_rows):
            prompt = str(prompt_arr[i].as_py())
            response = str(response_arr[i].as_py())
            fingerprint = dedup.fingerprint(prompt, response)
            if dedup.seen(fingerprint):
                continue
            dedup.add(fingerprint)
            out_f.write(json.dumps({"prompt": prompt, "response": response}) + "\n")
            written += 1

    return written

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: SHARD_ID=N python bin/worker.py manifests/latest.json", file=sys.stderr)
        sys.exit(1)

    manifest_path = sys.argv[1]
    with open(manifest_path) as f:
        manifest = json.load(f)

    date = manifest["date"]
    files = manifest["files"]

    out_dir = Path(__file__).parent.parent / "batches" / "public-merged" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{timestamp}.jsonl"

    dedup = DedupStore()
    total = 0

    with out_path.open("w") as out_f:
        for path in files:
            # Deterministic shard assignment by filename slug
            slug = Path(path).stem
            if not belongs_to_shard(slug):
                continue
            total += process_file(path, dedup, out_f)

    print(f"Shard {SHARD_ID}: wrote {total} records to {out_path}")

if __name__ == "__main__":
    main()
```

---

### 3) Update `.github/workflows/ingest.yml`
