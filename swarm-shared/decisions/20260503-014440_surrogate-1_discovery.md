# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** that:

- Uses a **single `list_repo_tree` snapshot** (JSON manifest) generated once per cron window to eliminate recursive API calls and rate limits.
- Downloads **only shard-assigned files** via CDN (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) **without Authorization** (public datasets).
- Projects heterogeneous schemas to **strict `{prompt, response}`** pairs at parse time; rejects malformed rows.
- Deduplicates via the **existing SQLite md5 store** (`lib/dedup.py`) unchanged.
- Writes **ordered, deterministic shards** to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Runs as a drop-in replacement for the 16-shard GitHub Actions matrix using `SHARD_ID` / `TOTAL_SHARDS`.

---

### Files to create/modify

1. `bin/manifest-snapshot.py` — one-off helper (run in CI before ingest) to produce `manifest.json`.
2. `bin/dataset-enrich.py` — new worker (replaces shell script).
3. `.github/workflows/ingest.yml` — add manifest generation step and pass to matrix.
4. `requirements.txt` — ensure `requests`, `datasets`, `pyarrow`, `numpy`, `tqdm`, `huggingface-hub`.

---

### `bin/manifest-snapshot.py`

```python
#!/usr/bin/env python3
"""
Generate a flat manifest for a repo/path to avoid recursive HF API calls
during ingestion.

Usage:
  HF_TOKEN=... python bin/manifest-snapshot.py \
    --repo axentx/surrogate-1-training-pairs \
    --path raw/2026-05-03 \
    --out manifest.json
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi, login

RATE_LIMIT_WAIT = 360

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--path", default="")
    parser.add_argument("--out", default="manifest.json")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if token:
        login(token=token)

    api = HfApi()
    entries = []
    cursor = None

    while True:
        try:
            tree = api.list_repo_tree(
                repo_id=args.repo,
                path=args.path or None,
                recursive=False,
                cursor=cursor,
            )
        except Exception as e:
            if "429" in str(e):
                print(f"Rate limited, waiting {RATE_LIMIT_WAIT}s", file=sys.stderr)
                time.sleep(RATE_LIMIT_WAIT)
                continue
            raise

        for item in tree:
            if item.rfilename.endswith((".jsonl", ".parquet", ".json")):
                entries.append(item.rfilename)

        cursor = tree.next_cursor
        if not cursor:
            break

    manifest = {
        "repo": args.repo,
        "path": args.path or "",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": sorted(set(entries)),
    }

    Path(args.out).write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest['files'])} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven CDN-bypass ingestion worker.

Environment:
  SHARD_ID=0..(TOTAL_SHARDS-1)
  TOTAL_SHARDS=16
  HF_TOKEN=...
  MANIFEST_PATH=manifest.json
"""
import json
import os
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
BATCH_DIR = Path("batches/public-merged")

def shard_filter(items, shard_id: int, total_shards: int):
    for item in items:
        h = int(hashlib.md5(item.encode("utf-8")).hexdigest(), 16)
        if h % total_shards == shard_id:
            yield item

def download_cdn(path: str, repo: str, timeout: int = 30) -> bytes:
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content

def parse_file(content: bytes, path: str):
    """Project heterogeneous file to {prompt,response} pairs."""
    suffix = Path(path).suffix.lower()
    pairs = []

    if suffix == ".parquet":
        tbl = pq.read_table(pa.BufferReader(content))
        cols = set(tbl.column_names)
        prompt_col = next((c for c in ("prompt", "instruction", "input") if c in cols), None)
        response_col = next((c for c in ("response", "output", "completion") if c in cols), None)

        if prompt_col and response_col:
            prompts = tbl[prompt_col].to_pylist()
            responses = tbl[response_col].to_pylist()
            for p, r in zip(prompts, responses):
                if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                    pairs.append({"prompt": p.strip(), "response": r.strip()})

    elif suffix in (".jsonl", ".json"):
        if suffix == ".json":
            data = json.loads(content)
            if isinstance(data, dict):
                data = [data]
        else:
            data = [
                json.loads(l)
                for l in content.decode("utf-8").strip().splitlines()
                if l.strip()
            ]

        for obj in data:
            if isinstance(obj, dict):
                prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input")
                response = obj.get("response") or obj.get("output") or obj.get("completion")
                if isinstance(prompt, str) and isinstance(response, str) and prompt.strip() and response.strip():
                    pairs.append({"prompt": prompt.strip(), "response": response.strip()})
    return pairs

def main() -> None:
    shard_id = int(os.environ.get("SHARD_ID", 0))
    total_shards = int(os.environ.get("TOTAL_SHARDS", 16))
    token = os.environ.get("HF_TOKEN", "")
    manifest_path = os.environ.get("MANIFEST_PATH", "manifest.json")

    BATCH_DIR.mkdir(parents=True, exist_ok=True)

    with open(manifest_path) as f:
        manifest = json.load(f)

    repo = manifest["repo"]
    files = manifest["files"]
    my_files = list(shard_filter(files, shard_id, total_shards))

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ts_str = datetime.now(timezone.utc).strftime("%H%M%S")
    out_path = BATCH_DIR / date_str / f"shard{shard_id}-{ts_str}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    dedup = DedupStore()
    written = 0
    skipped_dup = 0
    failed = 0

    for path in tqdm(my_files, desc=f"Shard {shard_id}"):
        try:
            content = download_cdn(path, repo)
        except Exception:
            failed += 1
            continue

        try:
            pairs = parse_file(content, path)
        except Exception:
            failed += 1
            continue

        for pair in pairs:
            text = f"{pair['prompt']}\n{pair['response']}"
            if dedup.seen(text):
                skipped_dup += 1
                continue

            with out_path.open("a") as f:
                f.write(json.dumps(pair, ensure_ascii=False) + "\n
