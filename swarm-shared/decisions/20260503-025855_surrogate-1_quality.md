# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads only assigned files via **HF CDN bypass** (`resolve/main/...`) — zero API calls during data load
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Dedups via central md5 store (`lib/dedup.py`)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Exits 0 on success; non-zero on fatal error (so Actions can retry)

Also update `.github/workflows/ingest.yml` to use a **manifest-first pattern** (build file list once, reuse across shards) and a matrix strategy for parallel shard execution.

---

### 1) Create new worker (`bin/dataset-enrich.py`)

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1.
Deterministic sharding + schema-safe projection + central dedup.

Usage:
  SHARD_ID=3 SHARD_TOTAL=16 DATE=2026-04-29 \
    HF_TOKEN=hf_xxx python3 bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
import datetime
from pathlib import Path
from typing import Iterator, Tuple

import requests
from huggingface_hub import HfApi, hf_hub_download

# ── config --
REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"
API = HfApi(token=os.getenv("HF_TOKEN"))

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.datetime.utcnow().strftime("%Y-%m-%d"))

OUT_DIR = Path("batches/public-merged") / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ── dedup --
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from dedup import DedupStore  # type: ignore

dedup = DedupStore()

# ── helpers --
def deterministic_shard(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def list_date_files(date_folder: str) -> list[str]:
    """Single API call: list top-level files in date folder."""
    items = API.list_repo_tree(repo_id=REPO_ID, path=date_folder, recursive=False)
    names = []
    for item in items:
        name = item.path if hasattr(item, "path") else item.get("path", "")
        if name:
            names.append(name)
    return sorted(names)

def cdn_url(repo_path: str) -> str:
    return f"{BASE_CDN}/{repo_path}"

def stream_cdn_lines(url: str) -> Iterator[bytes]:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

# ── schema-safe projection --
def extract_pair(raw: dict) -> Tuple[str, str] | None:
    """
    Conservative projection to {prompt, response}.
    Accepts common surrogate keys and falls back to first/second text-like fields.
    """
    if not isinstance(raw, dict):
        return None

    prompt = raw.get("prompt") or raw.get("instruction") or raw.get("input") or raw.get("question")
    response = raw.get("response") or raw.get("output") or raw.get("answer") or raw.get("completion")

    # fallback: pick first/second string fields
    if prompt is None or response is None:
        str_fields = [v for v in raw.values() if isinstance(v, str) and v.strip()]
        if len(str_fields) >= 2:
            prompt, response = str_fields[0], str_fields[1]
        else:
            return None

    prompt = str(prompt).strip()
    response = str(response).strip()
    if not prompt or not response:
        return None
    return prompt, response

# ── worker --
def process_file(repo_path: str) -> int:
    """Download via CDN, project, dedup, write. Returns written count."""
    written = 0
    url = cdn_url(repo_path)
    buffer = b""
    for chunk in stream_cdn_lines(url):
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except Exception:
                # tolerate non-json lines (skip)
                continue
            pair = extract_pair(raw)
            if pair is None:
                continue
            prompt, response = pair
            # dedup key: md5 of normalized pair
            key = hashlib.md5(f"{prompt}\n{response}".encode()).hexdigest()
            if dedup.seen(key):
                continue
            dedup.add(key)
            out = {"prompt": prompt, "response": response}
            with OUT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
            written += 1
    return written

def main() -> None:
    try:
        files = list_date_files(DATE)
    except Exception as exc:
        print(f"FATAL: failed to list files for {DATE}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not files:
        print(f"No files found for {DATE}")
        sys.exit(0)

    assigned = [f for f in files if deterministic_shard(f) == SHARD_ID]
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} processing {len(assigned)} files from {DATE}")

    total = 0
    for repo_path in assigned:
        try:
            n = process_file(repo_path)
            total += n
            print(f"  {repo_path} -> {n} pairs")
        except Exception as exc:
            print(f"  ERROR {repo_path}: {exc}", file=sys.stderr)

    print(f"Done. Wrote {total} pairs to {OUT_FILE}")

    # Push output to HF dataset repo (single commit per shard run)
    if total > 0:
        try:
            API.upload_file(
                path_or_fileobj=str(OUT_FILE),
                path_in_repo=str(OUT_FILE.relative_to(Path.cwd())),
                repo_id=REPO_ID,
                repo_type="dataset",
                commit_message=f"shard{SHARD_ID} {DATE} {TIMESTAMP} ({total} pairs)",
            )
            print("Uploaded to HF dataset.")
        except Exception as exc:
            print(f"FATAL: upload failed: {exc}", file=sys.stderr)
            sys.exit(1)

    sys.exit(0)

if __name__ == "__main__":
    main()
```

---

### 2) Keep Bash wrapper for cron compatibility (`bin/dataset-enrich.sh`)

```bash
#!/usr/bin/env bash
# Lightweight wrapper for cron/GitHub Actions.
# Ensures proper environment and invokes Python worker.

set -euo pipefail
export SHELL=/bin/bash

cd "$(dirname "$0")/.."

exec python3 bin/dataset-enrich.py "$@"
```

```bash
chmod +x bin/dataset-enrich.sh bin/dataset-enrich.py
```

---

### 3) Update `.github/workflows/ingest.yml`

```yaml
name: Ingest public pairs

on:
  workflow_dispatch:
    inputs:
