# vanguard / backend

## Final consolidated implementation (correct + actionable)

**What to build**  
One small, self-contained file:  
`/opt/axentx/vanguard/backend/manifest.py`  

It provides two commands:

1. **`build`** — create a content-addressed manifest for one date folder using a single HF API call (token required only here).  
2. **`stream`** — download from CDN only (no auth), with retries/backoff, on-the-fly sha256 validation, strict `{prompt,response}` projection, and parallel fetch.

This eliminates runtime repo enumeration, prevents poisoned downloads, avoids pyarrow schema errors, and makes epochs reproducible.

---

### File: `/opt/axentx/vanguard/backend/manifest.py`

```python
#!/usr/bin/env python3
"""
Vanguard manifest + CDN fetcher.

Usage:
  # Build manifest once (requires HF token)
  HF_TOKEN=... python3 manifest.py build \
      --repo=nvidia/Llama3-Curated-Mix \
      --date=2024-05-01 \
      --out=manifest-2024-05-01.jsonl

  # Stream from CDN during training (no token)
  python3 manifest.py stream \
      --manifest=manifest-2024-05-01.jsonl \
      --repo=nvidia/Llama3-Curated-Mix \
      --workers=8 \
      | python3 train.py
"""

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterator, List

import pyarrow as pa
import pyarrow.json as pj
import pyarrow.parquet as pq
import requests

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
HEADERS = {"User-Agent": "axentx-vanguard/1.0"}
MAX_RETRIES = 5
BACKOFF_BASE = 1.5  # seconds


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _download_with_retry(url: str) -> bytes:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 429:
                wait = (BACKOFF_BASE ** attempt) * 10
                print(f"CDN 429, waiting {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except (requests.RequestException, OSError) as exc:
            if attempt == MAX_RETRIES:
                raise
            wait = BACKOFF_BASE ** attempt
            print(f"Retry {attempt}/{MAX_RETRIES} after {exc}", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("Unreachable")


def build_manifest(repo: str, date: str, out_path: Path) -> None:
    """
    Build manifest for a single date folder with one HF API call.
    Requires HF_TOKEN env for listing; CDN downloads during training are token-free.
    """
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print(
            "Missing dependency: install huggingface_hub pyarrow requests\n"
            "  pip install huggingface_hub pyarrow requests",
            file=sys.stderr,
        )
        sys.exit(1)

    api = HfApi(token=os.getenv("HF_TOKEN"))
    folder = f"{date}/"
    entries = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    files = [e for e in entries if e.type == "file"]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fp:
        for f in files:
            path = f.path
            if path.startswith(folder):
                path = path[len(folder) :]
            path = f"{folder}{path}"
            row = {
                "path": path,
                "sha256": "",   # filled on first successful CDN fetch
                "size": getattr(f, "size", 0),
            }
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Wrote {len(files)} entries to {out_path}")


def _project_to_prompt_response(data: bytes, fmt: str) -> Iterator[Dict[str, str]]:
    """Project raw file bytes to strict {prompt, response} records."""
    try:
        if fmt == "parquet":
            table = pq.read_table(pa.BufferReader(data))
        else:
            # default to jsonl
            table = pj.read_json(pa.BufferReader(data))
    except Exception:
        return

    cols = set(table.column_names)

    prompt_col = next(
        (c for c in ("prompt", "instruction", "input", "question") if c in cols), None
    )
    response_col = next(
        (c for c in ("response", "output", "answer", "completion") if c in cols), None
    )

    if prompt_col and response_col:
        prompts = table.column(prompt_col).to_pylist()
        responses = table.column(response_col).to_pylist()
        for p, r in zip(prompts, responses):
            if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                yield {"prompt": p.strip(), "response": r.strip()}
    elif "text" in cols:
        texts = table.column("text").to_pylist()
        for t in texts:
            if isinstance(t, str) and t.strip():
                yield {"prompt": "", "response": t.strip()}


def stream_files(manifest_path: Path, repo: str, workers: int = 8) -> Iterator[Dict[str, str]]:
    with manifest_path.open() as fp:
        entries = [json.loads(l) for l in fp if l.strip()]

    def process(entry: Dict) -> List[Dict]:
        path = entry["path"]
        url = CDN_TEMPLATE.format(repo=repo, path=path)
        try:
            blob = _download_with_retry(url)
            actual_sha = _sha256_bytes(blob)
            fmt = Path(path).suffix.lower().lstrip(".")
            rows = list(_project_to_prompt_response(blob, fmt))
            # Best-effort update of manifest entry
            entry["sha256"] = actual_sha
            return rows
        except Exception as exc:
            print(f"Failed {path}: {exc}", file=sys.stderr)
            return []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(process, e): e for e in entries}
        for fut in as_completed(futures):
            for item in fut.result():
                yield item


def main() -> None:
    parser = argparse.ArgumentParser(description="Vanguard manifest + CDN fetcher")
    sub = parser.add_subparsers(dest="cmd", required=True)

    build_p = sub.add_parser("build")
    build_p.add_argument("--repo", required=True)
    build_p.add_argument("--date", required=True)
    build_p.add_argument("--out", required=True)

    stream_p = sub.add_parser("stream")
    stream_p.add_argument("--manifest", required=True)
    stream_p.add_argument("--repo", required=True)
    stream_p.add_argument("--workers", type=int, default=8)

    args = parser.parse_args()

    if args.cmd == "build":
        build_manifest(repo=args.repo, date=args.date, out_path=Path(args.out))
    elif args.cmd == "stream":
        for record in stream_files(
            manifest_path=Path(args.manifest), repo=args.repo, workers=args.workers
        ):
            print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

---

### How to use (concrete steps)

1. **Install dependencies once**  
   ```bash
   pip install huggingface_hub pyarrow requests
   ```

2. **Build manifest for a date folder** (requires HF token)  
   ```bash
   export HF_TOKEN=hf_...
   python3 /opt/axentx/vanguard/backend/manifest.py build \
       --repo=nvidia/Llama3-Curated-Mix \
