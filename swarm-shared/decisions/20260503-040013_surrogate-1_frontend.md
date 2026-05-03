# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **manifest-first strategy**:
  - If `manifest/{DATE_FOLDER}.json` exists, use it (avoids recursive API calls and rate limits).
  - Otherwise, fall back to a **single non-recursive `list_repo_tree` API call** to list the date folder and cache it as `manifest/{DATE_FOLDER}.json`.
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`.
- Downloads only assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header.
- Projects each file to `{prompt, response}` at parse time (avoids `load_dataset(streaming=True)` on mixed schemas).
- Deduplicates via central `lib/dedup.py` md5 store.
- Outputs `batches/public-merged/{DATE_FOLDER}/shard{SHARD_ID}-{HHMMSS}.jsonl` and uploads to the dataset repo via HF API.
- Reuses existing GitHub Actions matrix (`ingest.yml`) unchanged.

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker (manifest-first).

Usage (GitHub Actions matrix):
  SHARD_ID=3 SHARD_TOTAL=16 python bin/dataset-enrich.py

Env:
  HF_TOKEN         - write token for axentx/surrogate-1-training-pairs
  DATE_FOLDER      - e.g. 2026-05-03 (default: today)
  DATASET_REPO     - default: axentx/surrogate-1-training-pairs
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
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())

API = HfApi(token=HF_TOKEN)

# ── helpers --
def deterministic_shard(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def ensure_manifest() -> list[str]:
    """
    Use manifest/{DATE_FOLDER}.json if present.
    Otherwise, list date folder once (non-recursive) and cache it.
    Returns list of file paths (relative to dataset root).
    """
    manifest_dir = Path("manifest")
    manifest_dir.mkdir(exist_ok=True)
    manifest_path = manifest_dir / f"{DATE_FOLDER}.json"

    if manifest_path.is_file():
        with manifest_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            return data

    print(f"[shard {SHARD_ID}] manifest not found or invalid; listing {DATE_FOLDER} ...")
    items = API.list_repo_tree(
        repo_id=DATASET_REPO,
        path=DATE_FOLDER,
        repo_type="dataset",
        recursive=False,
    )
    files = [it.rfilename for it in items if it.type == "file"]

    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(files, f)

    return files

def project_to_pair(raw_obj) -> dict | None:
    """
    Best-effort projection to {prompt, response}.
    Accepts dict-like or HF dataset row.
    """
    d = dict(raw_obj) if not isinstance(raw_obj, dict) else raw_obj

    prompt_keys = {"prompt", "instruction", "input", "question", "text"}
    response_keys = {"response", "completion", "output", "answer", "result"}

    prompt = None
    response = None

    for k in d:
        if k in prompt_keys and prompt is None:
            prompt = str(d[k]).strip()
        if k in response_keys and response is None:
            response = str(d[k]).strip()

    if prompt is None and response is None:
        for k in d:
            if isinstance(d[k], str) and d[k].strip():
                prompt = d[k].strip()
                response = ""
                break

    if prompt is None:
        return None
    if response is None:
        response = ""

    return {"prompt": prompt, "response": response}

# ── dedup --
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa

dedup = DedupStore()

# ── main --
def main() -> None:
    out_dir = Path("batches/public-merged") / DATE_FOLDER
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    outfile = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"

    files = ensure_manifest()
    assigned = [f for f in files if deterministic_shard(f) == SHARD_ID]
    print(f"[shard {SHARD_ID}/{SHARD_TOTAL}] assigned {len(assigned)} files")

    written = 0
    skipped_dup = 0
    skipped_proj = 0

    with outfile.open("w", encoding="utf-8") as fout:
        for rel_path in assigned:
            # Prefer CDN-bypass streaming when possible.
            # Use hf_hub_download for reliable format decoding (parquet/jsonl/etc).
            local_path = hf_hub_download(
                repo_id=DATASET_REPO,
                filename=rel_path,
                repo_type="dataset",
                token=HF_TOKEN,
            )

            try:
                from datasets import load_dataset

                ds = load_dataset("json", data_files=local_path, split="train")
                for row in ds:
                    pair = project_to_pair(row)
                    if pair is None:
                        skipped_proj += 1
                        continue

                    payload = json.dumps(pair, sort_keys=True, separators=(",", ":"))
                    md5 = hashlib.md5(payload.encode()).hexdigest()
                    if dedup.exists(md5):
                        skipped_dup += 1
                        continue

                    dedup.add(md5)
                    fout.write(json.dumps(pair, ensure_ascii=False) + "\n")
                    written += 1
            except Exception as exc:
                print(f"  WARN failed to project {rel_path}: {exc}", file=sys.stderr)
                skipped_proj += 1

    print(f"[shard {SHARD_ID}] written={written} skipped_dup={skipped_dup} skipped_proj={skipped_proj}")

    # upload output file to dataset repo
    print(f"[shard {SHARD_ID}] uploading {outfile} ...")
    API.upload_file(
        path_or_fileobj=str(outfile),
        path_in_repo=f"batches/public-merged/{DATE_FOLDER}/{outfile.name}",
        repo_id=DATASET_REPO,
        repo_type="dataset",
        commit_message=f"shard{SHARD_ID} {ts} public-merged",
    )
    print("[done]")

if __name__ == "__main__":
    main()
```

### 2) Update `bin/dataset-enrich.sh` → thin wrapper (optional)

Keep backward compatibility for any local/manual calls:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec python "$(dirname "$0")/dataset-enrich.py" "$@"
```

`chmod +x bin/dataset-enrich.sh bin/dataset-enrich.py`

### 3) GitHub Actions: no changes required

Existing matrix in `.github/workflows/ingest.yml` already provides `SHARD_ID`/`SH
