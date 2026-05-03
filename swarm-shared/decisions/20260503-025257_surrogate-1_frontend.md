# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list
- Shard assignment by `hash(slug) % SHARD_TOTAL`
- Downloads via **HF CDN bypass** (`resolve/main/...`) — zero API calls during data load
- Projects to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Dedup via central `lib/dedup.py` md5 store
- Outputs `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Reuses existing Lightning Studio pattern: list running studios before creating new ones (saves quota)
- Adds proper Bash shebang + executable bit for any wrapper scripts (prevents cron/opus-pr-reviewer errors)

---

## Step-by-step (1h 45m total)

1. **Create new Python worker** (`bin/dataset-enrich.py`) — 60m
2. **Update GitHub Actions workflow** (`ingest.yml`) to use Python + matrix — 30m
3. **Add wrapper script** (`bin/run-enrich.sh`) with proper shebang + executable bit — 10m
4. **Smoke test** locally + push — 5m

---

## Code Snippets

### 1. `bin/dataset-enrich.py` (new)

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage:
  HF_TOKEN=hf_xxx \
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  python bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

REPO = "datasets/axentx/surrogate-1-training-pairs"
API = HfApi()

def slug_from_path(path: str) -> str:
    # e.g. "2026-05-03/abc123.parquet" -> "abc123"
    return Path(path).stem

def deterministic_shard(slug: str, total: int) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % total

def cdn_download(repo: str, path: str, out_path: Path):
    """Download via HF CDN (no auth/rate-limit on resolve/main)."""
    url = f"https://huggingface.co/{repo}/resolve/main/{path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    out_path.write_bytes(resp.content)

def project_to_pair(raw_bytes: bytes, file_path: str):
    """
    Lightweight projection to {prompt,response}.
    Supports .jsonl and .parquet via pyarrow only when needed.
    """
    path = Path(file_path)
    if path.suffix == ".jsonl":
        # assume one {prompt,response} per line
        import jsonlines
        pairs = []
        with jsonlines.Reader(raw_bytes.decode().splitlines()) as reader:
            for obj in reader:
                if "prompt" in obj and "response" in obj:
                    pairs.append({"prompt": obj["prompt"], "response": obj["response"]})
        return pairs

    elif path.suffix == ".parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq
        table = pq.read_table(pa.BufferReader(raw_bytes))
        cols = set(table.column_names)
        if "prompt" in cols and "response" in cols:
            df = table.select(["prompt", "response"]).to_pandas()
            return df.to_dict(orient="records")
        # fallback: try to find any string/string pair
        for c1 in table.column_names:
            for c2 in table.column_names:
                if c1 != c2 and table.schema.field(c1).type in (pa.string(), pa.large_string()) \
                   and table.schema.field(c2).type in (pa.string(), pa.large_string()):
                    df = table.select([c1, c2]).to_pandas()
                    df.columns = ["prompt", "response"]
                    return df.to_dict(orient="records")
        return []
    else:
        return []

def main():
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    hf_token = os.getenv("HF_TOKEN")
    if not hf_token:
        print("HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    # 1) list files once (single API call)
    print(f"[{shard_id}] listing {REPO}/{date} ...")
    try:
        tree = list_repo_tree(REPO, path=date, recursive=False, token=hf_token)
    except Exception as e:
        # If rate-limited, rely on cached file list if provided via env
        cached = os.getenv("CACHED_FILE_LIST")
        if cached:
            tree = [type("obj", (object,), {"path": p.strip()})() for p in cached.strip().split("\n") if p.strip()]
        else:
            print(f"[{shard_id}] list_repo_tree failed: {e}", file=sys.stderr)
            sys.exit(1)

    files = [t.path for t in tree if t.path]
    my_files = [f for f in files if deterministic_shard(slug_from_path(f), shard_total) == shard_id]
    print(f"[{shard_id}] assigned {len(my_files)} files out of {len(files)}")

    # 2) import dedup lazily
    sys.path.insert(0, str(Path(__file__).parent))
    from lib.dedup import DedupStore
    dedup = DedupStore()

    # 3) process
    out_dir = Path(f"batches/public-merged/{date}")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    out_path = out_dir / f"shard{shard_id}-{ts}.jsonl"

    written = 0
    skipped_dup = 0
    with out_path.open("w", encoding="utf-8") as fout:
        for i, file_path in enumerate(my_files):
            try:
                # CDN bypass download
                raw = requests.get(
                    f"https://huggingface.co/{REPO}/resolve/main/{file_path}",
                    timeout=30
                ).content
                pairs = project_to_pair(raw, file_path)
                for pair in pairs:
                    prompt = str(pair.get("prompt", "")).strip()
                    response = str(pair.get("response", "")).strip()
                    if not prompt or not response:
                        continue
                    # dedup by content hash
                    md5 = hashlib.md5((prompt + "\n" + response).encode()).hexdigest()
                    if dedup.exists(md5):
                        skipped_dup += 1
                        continue
                    dedup.add(md5)
                    fout.write(json.dumps({"prompt": prompt, "response": response}, ensure_ascii=False) + "\n")
                    written += 1
            except Exception as e:
                print(f"[{shard_id}] error processing {file_path}: {e}", file=sys.stderr)

            if (i + 1) % 10 == 0:
                print(f"[{shard_id}] processed {i+1}/{len(my_files)} files")

        fout.flush()

    print(f"[{shard_id}] done. written={written} skipped_dup={skipped_dup} out={out_path}")

    # 4) upload via huggingface_hub (HF_TOKEN must have write access)
    print(f"[{shard_id}] uploading to {REPO} ...")
    API.upload_file(
        path_or_fileobj=str(out_path),
        path_in_repo=str(out_path),
        repo_id=REPO.replace("datasets/", ""),
        repo_type="dataset",
        token=hf_token,
    )
   
