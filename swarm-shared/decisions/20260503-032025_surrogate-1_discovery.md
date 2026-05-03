# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Single `list_repo_tree(path, recursive=False)` per date folder
- Saves manifest JSON (file paths) once; worker uses CDN URLs (`resolve/main/...`) for all downloads (bypasses `/api/` rate limits)
- Projects each file to `{prompt, response}` at parse time (avoids pyarrow CastError from mixed schemas)
- Deterministic sharding: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Central dedup via `lib/dedup.py` (md5 store)
- Output: `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- No `source`/`ts` columns in records; attribution via filename pattern only
- Reusable across cron and `workflow_dispatch`

---

### Code Snippets

#### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage (env):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 HF_TOKEN=hf_xxx python bin/dataset-enrich.py
"""
import os
import sys
import json
import hashlib
import datetime
from pathlib import Path

import requests
from huggingface_hub import HfApi, hf_hub_download

REPO = "axentx/surrogate-1-training-pairs"
API = HfApi(token=os.getenv("HF_TOKEN"))

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.datetime.utcnow().strftime("%Y-%m-%d"))
OUT_DIR = Path("batches/public-merged") / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
TS = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TS}.jsonl"

# Central dedup store (shared across runners via HF repo file)
DEDUP_FILE = "batches/.dedup-md5.jsonl"

def deterministic_shard(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def list_date_files(date: str):
    """Single API call: list top-level files for date folder (non-recursive)."""
    items = API.list_repo_tree(repo_id=REPO, path=date, recursive=False)
    # Keep only files (skip nested folders)
    files = [it.rfilename for it in items if it.type == "file"]
    return files

def cdn_download_url(repo: str, path: str) -> str:
    """CDN URL that bypasses HF API auth/rate limits."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def project_to_pair(raw_obj) -> dict:
    """
    Project heterogeneous file to {prompt, response} only.
    Accepts dict/list/str and normalizes.
    """
    if isinstance(raw_obj, dict):
        prompt = raw_obj.get("prompt") or raw_obj.get("input") or raw_obj.get("question") or ""
        response = raw_obj.get("response") or raw_obj.get("output") or raw_obj.get("answer") or ""
        return {"prompt": str(prompt).strip(), "response": str(response).strip()}
    if isinstance(raw_obj, list) and len(raw_obj) >= 2:
        return {"prompt": str(raw_obj[0]).strip(), "response": str(raw_obj[1]).strip()}
    # fallback: treat as single text -> split by common separators
    text = str(raw_obj).strip()
    if "\n\n" in text:
        parts = text.split("\n\n", 1)
    elif "\n" in text:
        parts = text.split("\n", 1)
    else:
        parts = [text, ""]
    return {"prompt": parts[0].strip(), "response": parts[1].strip() if len(parts) > 1 else ""}

def load_dedup_set():
    try:
        if os.path.exists(DEDUP_FILE):
            with open(DEDUP_FILE) as f:
                return {line.strip() for line in f if line.strip()}
    except Exception:
        pass
    return set()

def append_dedup(md5s):
    # Best-effort append; collisions handled by central store on HF repo
    try:
        with open(DEDUP_FILE, "a") as f:
            for m in md5s:
                f.write(m + "\n")
    except Exception:
        pass

def main():
    print(f"[shard={SHARD_ID}] processing date={DATE}", file=sys.stderr)

    files = list_date_files(DATE)
    print(f"[shard={SHARD_ID}] found {len(files)} files", file=sys.stderr)

    my_files = [f for f in files if deterministic_shard(f) == SHARD_ID]
    print(f"[shard={SHARD_ID}] assigned {len(my_files)} files", file=sys.stderr)

    dedup = load_dedup_set()
    written = 0
    skipped_dup = 0
    new_hashes = []

    with OUT_FILE.open("w") as out:
        for rel_path in my_files:
            full_path = f"{DATE}/{rel_path}"
            # Use CDN URL to bypass API rate limits during download
            url = cdn_download_url(REPO, full_path)
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
            except Exception as exc:
                print(f"[shard={SHARD_ID}] WARN failed to fetch {full_path}: {exc}", file=sys.stderr)
                continue

            # Try parse as JSON lines or JSON array
            content = resp.content
            lines = []
            try:
                # Try JSON lines
                for line in content.decode("utf-8").splitlines():
                    line = line.strip()
                    if line:
                        obj = json.loads(line)
                        lines.append(obj)
            except Exception:
                try:
                    # Try single JSON array/object
                    obj = json.loads(content.decode("utf-8"))
                    if isinstance(obj, list):
                        lines = obj
                    else:
                        lines = [obj]
                except Exception as exc2:
                    print(f"[shard={SHARD_ID}] WARN cannot parse {full_path}: {exc2}", file=sys.stderr)
                    continue

            for item in lines:
                pair = project_to_pair(item)
                if not pair["prompt"] and not pair["response"]:
                    continue

                # Dedup by content hash
                payload = json.dumps(pair, sort_keys=True, separators=(",", ":"))
                md5 = hashlib.md5(payload.encode()).hexdigest()
                if md5 in dedup:
                    skipped_dup += 1
                    continue

                dedup.add(md5)
                new_hashes.append(md5)
                out.write(payload + "\n")
                written += 1

    # Best-effort update central dedup store
    if new_hashes:
        append_dedup(new_hashes)

    print(f"[shard={SHARD_ID}] done: written={written} skipped_dup={skipped_dup} out={OUT_FILE}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

#### `bin/dataset-enrich.sh` (wrapper for backward compatibility)
```bash
#!/usr/bin/env bash
# Thin wrapper to invoke the Python worker with expected env.
# Keeps cron/workflow compatibility while delegating to CDN-bypass logic.

set -euo pipefail
export SHELL=/bin/bash

exec python3 "$(dirname "$0")/dataset-enrich.py" "$@"
```

#### Update workflow (`.github/workflows/ingest.yml`) snippet
```yaml
# Ensure matrix uses SHARD_ID and passes DATE
jobs:
  ingest:
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      -
