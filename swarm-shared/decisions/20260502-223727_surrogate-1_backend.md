# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What we’ll do
1. Add `bin/list-files.py` — one-time Mac/CI script that calls `list_repo_tree` once per date folder and writes `file-list.json` (path + size + sha256). This list is committed/embedded so training and all 16 shards use CDN URLs only (zero API calls during data load).
2. Update `bin/dataset-enrich.sh` to accept an optional `FILE_LIST` env var; if provided, workers stream from CDN URLs in that list instead of calling `load_dataset`/`list_repo_files`. Fallback to legacy behavior if not provided.
3. Add `bin/train-cdn.sh` — Lightning launcher that embeds the file list and runs CDN-only training (no HF dataset streaming, no `list_repo_files`). Uses `hf_hub_download`-style CDN URLs with `requests`/`urllib` and `pyarrow` projection to `{prompt, response}`.

---

### 1) `bin/list-files.py`

Deterministic pre-flight lister with integrity checks. Run from Mac/CI after rate-limit window clears; commit `file-list.json` or pass to workers.

```python
#!/usr/bin/env python3
"""
List public dataset files once and emit file-list.json with integrity metadata.
Usage:
  HF_TOKEN=... python bin/list-files.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 \
    --out file-list.json
"""
import argparse
import hashlib
import json
import os
import sys
from huggingface_hub import HfApi, login

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192 * 16), b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="Date folder under data/ (e.g. 2026-05-02)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"))
    parser.add_argument("--cache-dir", default=os.getenv("HF_HOME", os.path.expanduser("~/.cache/huggingface")))
    args = parser.parse_args()

    if not args.token:
        print("ERROR: HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    login(token=args.token)
    api = HfApi()

    # Non-recursive per folder to avoid 100x pagination and 429s
    prefix = f"data/{args.date}/"
    entries = api.list_repo_tree(repo_id=args.repo, path=prefix, recursive=False)

    files = []
    cache_root = os.path.join(args.cache_dir, "datasets", args.repo, "resolve", "main")
    os.makedirs(cache_root, exist_ok=True)

    for entry in entries:
        if entry.type != "file":
            continue
        # CDN URL (no auth, bypasses /api/ rate limit)
        cdn_url = f"https://huggingface.co/datasets/{args.repo}/resolve/main/{entry.path}"
        # Local cache path (hf_hub_download style)
        cache_path = os.path.join(cache_root, os.path.basename(entry.path))
        sha256 = None
        if os.path.exists(cache_path):
            sha256 = sha256_file(cache_path)
        files.append(
            {
                "path": entry.path,
                "cdn_url": cdn_url,
                "size": getattr(entry, "size", None),
                "sha256": sha256,
                "cache_path": cache_path,
            }
        )

    payload = {"repo": args.repo, "date": args.date, "files": files}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files -> {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list-files.py
```

---

### 2) `bin/cdn_reader.py`

Lightweight helper to stream from CDN (with caching) and project to `{prompt, response}` without `datasets` schema issues.

```python
#!/usr/bin/env python3
"""
Stream parquet/jsonl from CDN (with local caching) and yield {prompt, response}.
Usage:
  python bin/cdn_reader.py file1.parquet file2.jsonl ...
"""
import pyarrow.parquet as pq
import json
import sys
import urllib.request
from pathlib import Path
from typing import Iterator, Dict, Any
import tempfile
import os
import hashlib

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192 * 16), b""):
            h.update(chunk)
    return h.hexdigest()

def cdn_stream_to_file(cdn_url: str, tmpdir: str, expected_sha256: str = None) -> Path:
    """Download via CDN to tmpdir; return local path. Validate sha256 if provided."""
    out = Path(tmpdir) / Path(cdn_url).name
    if out.exists():
        if expected_sha256 and sha256_file(str(out)) != expected_sha256:
            out.unlink()
        else:
            return out
    req = urllib.request.Request(cdn_url, headers={"User-Agent": "axentx-surrogate-1"})
    with urllib.request.urlopen(req) as resp, open(out, "wb") as f:
        while chunk := resp.read(8192 * 16):
            f.write(chunk)
    if expected_sha256 and sha256_file(str(out)) != expected_sha256:
        out.unlink()
        raise ValueError(f"SHA256 mismatch for {cdn_url}")
    return out

def read_parquet_pairs(path: Path) -> Iterator[Dict[str, Any]]:
    try:
        table = pq.read_table(path, columns=["prompt", "response"])
    except Exception:
        # fallback: read all and project
        table = pq.read_table(path)
    for col in ("prompt", "response"):
        if col not in table.column_names:
            raise ValueError(f"Missing column {col} in {path}")
    for i in range(table.num_rows):
        row = {col: table[col][i].as_py() for col in ("prompt", "response")}
        yield row

def read_jsonl_pairs(path: Path) -> Iterator[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            yield {"prompt": obj["prompt"], "response": obj["response"]}

def stream_pairs(cdn_urls_with_meta):
    with tempfile.TemporaryDirectory() as td:
        for item in cdn_urls_with_meta:
            url = item["cdn_url"]
            sha256 = item.get("sha256")
            local_path = cdn_stream_to_file(url, td, expected_sha256=sha256)
            if local_path.suffix == ".parquet":
                yield from read_parquet_pairs(local_path)
            elif local_path.suffix == ".jsonl":
                yield from read_jsonl_pairs(local_path)
            else:
                print(f"skip unsupported {local_path}", file=sys.stderr)

if __name__ == "__main__":
    # Accept JSON lines of metadata or raw URLs
    items = []
    for arg in sys.argv[1:]:
        if arg.endswith(".json"):
            data = json.load(open(arg))
            items.extend(data.get("files", []))
        else:
            items.append({"cdn_url": arg})
    for pair in stream_pairs(items):
        print(json.dumps(pair, ensure_ascii=False))
```

Make executable:

```bash

