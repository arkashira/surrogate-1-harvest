# surrogate-1 / quality

## Final Implementation (merged + corrected)

Replace `bin/dataset-enrich.sh` with a single, deterministic, CDN-bypass worker that:

- Uses a **pre-computed manifest** (generated once per date on the Mac orchestrator) listing files in `raw/<date>/`.
- Each GitHub Actions shard (`SHARD_ID=0..15`, `SHARD_TOTAL=16`) receives the manifest and processes **only its 1/16 slice** determined by `md5(filename) % SHARD_TOTAL`.
- Downloads via **public CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) with **zero HF API calls during ingestion** and no `Authorization` header required.
- Projects heterogeneous schemas to `{prompt, response}` **at parse time**; no schema pollution (no `source`, `ts` columns). Attribution is encoded in output filename pattern.
- Deduplicates via the central `lib/dedup.py` md5 store.
- Writes deterministic output: `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Handles HF CDN/network errors with exponential backoff + jitter and retries.
- Reuses an existing Lightning Studio session when present; does not recreate Studio environments.

---

### 1) Manifest generator (Mac orchestrator) — 15m

`bin/gen-manifest.py`

```python
#!/usr/bin/env python3
"""
Generate a manifest of public dataset files for a single date folder.
Run once per date on the Mac orchestrator (or any machine with HF token).

Usage:
  HF_TOKEN=hf_xxx python bin/gen-manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out manifest-2026-05-03.json
"""
import argparse
import json
import os
import sys
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("ERROR: huggingface_hub not installed. Run: pip install huggingface_hub")
    sys.exit(1)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser    .add_argument("--date", required=True, help="YYYY-MM-DD folder under raw/")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi(token=os.getenv("HF_TOKEN"))
    entries = api.list_repo_tree(
        repo_id=args.repo,
        path=f"raw/{args.date}",
        repo_type="dataset",
        recursive=False,
    )

    files = sorted(e.path for e in entries if e.type == "file")
    manifest = {
        "repo": args.repo,
        "date": args.date,
        "files": files,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

```bash
chmod +x bin/gen-manifest.py
```

---

### 2) Worker script (replaces `bin/dataset-enrich.sh`) — 60m

`bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven CDN-bypass ingestion worker.

Environment (passed by GitHub Actions):
  SHARD_ID=0..15
  SHARD_TOTAL=16
  MANIFEST_PATH=manifest-2026-05-03.json
  HF_TOKEN=hf_xxx          (for central dedup store push)
  DATE=2026-05-03
"""
import argparse
import hashlib
import json
import os
import sys
import time
import random
from pathlib import Path
from typing import Any, Dict, List

import requests

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent / "lib"))
try:
    from dedup import DedupStore
except Exception:
    # Minimal fallback if lib/dedup.py unavailable
    class DedupStore:
        def __init__(self, path=":memory:"):
            self.seen = set()

        def exists(self, key: str) -> bool:
            return key in self.seen

        def add(self, key: str) -> None:
            self.seen.add(key)

        def flush(self):
            pass

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def hash_slug(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest(), 16)

def assign_shard(slug: str, total: int) -> int:
    return hash_slug(slug) % total

def safe_project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Heuristic: look for common field names; fallback to longest text-like fields.
    """
    prompt = None
    response = None

    for pkey in ("prompt", "instruction", "input", "question", "user"):
        if pkey in raw and isinstance(raw[pkey], str) and raw[pkey].strip():
            prompt = raw[pkey].strip()
            break
    for rkey in ("response", "output", "answer", "completion", "assistant"):
        if rkey in raw and isinstance(raw[rkey], str) and raw[rkey].strip():
            response = raw[rkey].strip()
            break

    if prompt is None or response is None:
        str_items = [(k, v) for k, v in raw.items() if isinstance(v, str) and v.strip()]
        str_items.sort(key=lambda x: len(x[1]), reverse=True)
        if len(str_items) >= 2:
            prompt, response = str_items[0][1].strip(), str_items[1][1].strip()
        elif len(str_items) == 1:
            prompt, response = str_items[0][1].strip(), ""
        else:
            prompt, response = "", ""

    return {"prompt": prompt, "response": response}

def download_with_retry(url: str, max_retries: int = 5) -> bytes:
    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 429:
                # Respect Retry-After if present
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else backoff
                print(f"429 Too Many Requests. Waiting {wait:.1f}s (attempt {attempt})")
                time.sleep(wait)
                backoff = min(backoff * 2 + random.uniform(0, 0.5), 60.0)
                continue
            resp.raise_for_status()
            return resp.content
        except (requests.RequestException, OSError) as exc:
            print(f"Attempt {attempt} failed: {exc}")
            if attempt == max_retries:
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2 + random.uniform(0, 0.5), 60.0)
    raise RuntimeError("Unreachable")

def parse_file(content: bytes, path: str) -> List[Dict[str, str]]:
    """
    Parse parquet/jsonl content and project to pairs.
    """
    suffix = Path(path).suffix.lower()
    pairs: List[Dict[str, str]] = []

    if suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
            import pyarrow as pa
        except ImportError:
            print("WARNING: pyarrow not installed; skipping parquet file")
            return []
        table = pq.read_table(pa.BufferReader(content))
        for batch in table.to_batches(max_chunksize=8192):
            cols = batch.column_names
            for i in range(batch.num_rows):
                raw = {c: batch[c][i].as_py() for c in cols}
                pairs.append(safe_project_to_pair(raw))
    elif suffix in (".jsonl", ".json"):
        text = content.decode("utf-8")
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
           
