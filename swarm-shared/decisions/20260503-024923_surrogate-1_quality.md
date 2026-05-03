# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list
- Shard assignment by `hash(slug) % SHARD_TOTAL`
- Per-file CDN download via `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{DATE}/{file}` (no auth → bypasses API 429)
- Schema-robust parse → project to `{prompt, response}` only
- Central dedup via existing `lib/dedup.py` (SQLite md5 store)
- Output: `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Idempotent: re-runs on same date skip already-processed slugs via dedup; collisions avoided by shard+timestamp filename

---

### 1) Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx python bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Tuple

import requests
from huggingface_hub import HfApi, hf_hub_download

REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

# Local imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa

api = HfApi(token=os.getenv("HF_TOKEN"))
dedup = DedupStore()

def list_date_files(date: str) -> list[str]:
    """Single API call: list files in DATE/ folder (non-recursive)."""
    items = api.list_repo_tree(repo_id=REPO_ID, path=date, recursive=False)
    names = []
    for it in items:
        name = it["path"] if isinstance(it, dict) else getattr(it, "path", str(it))
        if name.startswith(f"{date}/") and not name.endswith("/"):
            names.append(name)
    names.sort()
    return names

def shard_for(path: str, total: int) -> int:
    slug = path.split("/")[-1]
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return h % total

def cdn_url(path: str) -> str:
    return f"{BASE_CDN}/{path}"

def stream_download(url: str, chunk_size: int = 8192) -> Iterator[bytes]:
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    for chunk in resp.iter_content(chunk_size=chunk_size):
        if chunk:
            yield chunk

def parse_parquet_to_pairs(path: str) -> Iterator[Tuple[str, str]]:
    """
    Conservative projection: read only columns that map to prompt/response.
    Uses hf_hub_download (local cache) to avoid schema heterogeneity issues.
    """
    import pyarrow.parquet as pq
    local_path = hf_hub_download(repo_id=REPO_ID, filename=path, repo_type="dataset")
    pf = pq.read_table(local_path, columns=["prompt", "response"], use_threads=False)
    df = pf.to_pandas()
    for _, row in df.iterrows():
        prompt = str(row.get("prompt") or "").strip()
        response = str(row.get("response") or "").strip()
        if prompt and response:
            yield prompt, response

def parse_jsonl_to_pairs(content: bytes) -> Iterator[Tuple[str, str]]:
    for line in content.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        prompt = str(obj.get("prompt") or obj.get("input") or "").strip()
        response = str(obj.get("response") or obj.get("output") or "").strip()
        if prompt and response:
            yield prompt, response

def compute_md5(prompt: str, response: str) -> str:
    return hashlib.md5(f"{prompt}\0{response}".encode()).hexdigest()

def run_shard(date: str, shard_id: int, shard_total: int) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path("batches") / "public-merged" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard{shard_id}-{ts}.jsonl"

    files = list_date_files(date)
    my_files = [f for f in files if shard_for(f, shard_total) == shard_id]
    print(f"[shard{shard_id}] {len(my_files)} files assigned out of {len(files)} total", flush=True)

    written = 0
    skipped = 0
    with out_path.open("w", encoding="utf-8") as fout:
        for rel in my_files:
            try:
                # Try parquet first (schema heterogeneity handled by projection)
                pairs = list(parse_parquet_to_pairs(rel))
                if not pairs:
                    # Fallback: raw CDN JSONL download (bypasses API auth/limits)
                    url = cdn_url(rel)
                    content = b"".join(stream_download(url))
                    pairs = list(parse_jsonl_to_pairs(content))
            except Exception as exc:
                print(f"[shard{shard_id}] WARN failed {rel}: {exc}", flush=True)
                continue

            for prompt, response in pairs:
                md5 = compute_md5(prompt, response)
                if dedup.exists(md5):
                    skipped += 1
                    continue
                dedup.add(md5)
                record = {"prompt": prompt, "response": response, "source": rel}
                fout.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1

            # Periodic flush to avoid loss on early termination
            fout.flush()

    print(f"[shard{shard_id}] done: {written} written, {skipped} skipped -> {out_path}", flush=True)

    # Optional: upload output file to dataset repo (one commit per shard run)
    if written > 0 and os.getenv("HF_TOKEN"):
        try:
            api.upload_file(
                path_or_fileobj=str(out_path),
                path_in_repo=str(out_path.relative_to(Path.cwd())),
                repo_id=REPO_ID,
                repo_type="dataset",
                commit_message=f"shard{shard_id} {date} {ts} ({written} pairs)",
            )
            print(f"[shard{shard_id}] uploaded to repo", flush=True)
        except Exception as exc:
            print(f"[shard{shard_id}] WARN upload failed: {exc}", flush=True)

def main() -> None:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date = os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
    if not os.getenv("HF_TOKEN"):
        print("HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    run_shard(date=date, shard_id=shard_id, shard_total=shard_total)

if __name__ == "__main__":
    main()
```

```bash
chmod +x bin/dataset-enrich.py
```

---

### 2) Update GitHub Actions matrix to use Python worker

`.github/workflows/ingest.yml`

```yaml
name: Ingest (16-shard CDN-bypass)

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
    inputs:
      date:
        description: "Date (YYYY-MM-D
