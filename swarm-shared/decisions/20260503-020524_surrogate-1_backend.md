# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac orchestrator runs `list_repo_tree` once per date folder and commits it) to avoid recursive `list_repo_files` and HF API 429s.
- Downloads only assigned shard files via **HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) — no Authorization header, bypasses `/api` endpoints and HF dataset library rate limits.
- Projects heterogeneous schemas to `{prompt,response}` only at parse time (avoids PyArrow `CastError` and schema drift).
- Deduplicates via central `lib/dedup.py` md5 store (content-based, not row-based).
- Writes `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with no extra metadata columns (attribution via filename pattern and directory structure).
- Exits non-zero on unrecoverable errors so Actions marks shard failed; partial failures are logged but do not abort the shard.

---

## Concrete Changes

### 1) New worker script: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Environment:
  SHARD_ID      int  (0..SHARD_TOTAL-1)
  SHARD_TOTAL   int  (default 16)
  HF_DATASET    str  (default axentx/surrogate-1-training-pairs)
  FILE_LIST     str  (path to file-list.json, default file-list.json)
  DATE_STR      str  (YYYY-MM-DD, default today)
  HF_TOKEN      str  (write token for uploads)
"""

import os
import sys
import json
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import Dict, Any, List

import requests
import pyarrow.parquet as pq

# ── config ──────────────────────────────────────────────────────────────

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
HF_DATASET = os.getenv("HF_DATASET", "axentx/surrogate-1-training-pairs")
FILE_LIST = os.getenv("FILE_LIST", "file-list.json")
DATE_STR = os.getenv("DATE_STR", datetime.date.today().isoformat())
HF_TOKEN = os.getenv("HF_TOKEN", "")

# ── helpers ─────────────────────────────────────────────────────────────

def load_file_list(path: str) -> List[str]:
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict) and "files" in data:
        return data["files"]
    return data

def shard_filter(path: str) -> bool:
    slug = path.rstrip("/")
    bucket = hash(slug) % SHARD_TOTAL
    if bucket < 0:
        bucket = -bucket
    return bucket == SHARD_ID

def cdn_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main/{path}"

def download_file(path: str, local_path: Path) -> bool:
    url = cdn_url(path)
    try:
        r = requests.get(url, timeout=60, stream=True)
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as exc:
        print(f"[WARN] failed to download {path}: {exc}", file=sys.stderr)
        return False

def md5_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()

def extract_pair(obj: Dict[str, Any]) -> Dict[str, str]:
    """Project heterogeneous schema to {prompt, response}."""
    prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
    response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
    if isinstance(prompt, (list, dict)):
        prompt = json.dumps(prompt, ensure_ascii=False)
    if isinstance(response, (list, dict)):
        response = json.dumps(response, ensure_ascii=False)
    return {"prompt": str(prompt), "response": str(response)}

def parse_parquet(path: Path) -> List[Dict[str, str]]:
    try:
        table = pq.read_table(path, columns=["prompt", "response"])
    except Exception:
        table = pq.read_table(path)
    out = []
    for batch in table.to_batches():
        cols = batch.column_names
        # fast path when columns exist
        if "prompt" in cols and "response" in cols:
            prompts = batch.column("prompt").to_pylist()
            responses = batch.column("response").to_pylist()
            for p, r in zip(prompts, responses):
                out.append(extract_pair({"prompt": p, "response": r}))
        else:
            df = batch.to_pandas()
            for _, row in df.iterrows():
                out.append(extract_pair(row.to_dict()))
    return out

def parse_jsonl(path: Path) -> List[Dict[str, str]]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            out.append(extract_pair(obj))
    return out

# ── dedup bridge ────────────────────────────────────────────────────────

def is_duplicate(md5_hash: str) -> bool:
    proc = subprocess.run(
        [sys.executable, "lib/dedup.py", "exists", md5_hash],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0

def register_md5(md5_hash: str) -> None:
    subprocess.run(
        [sys.executable, "lib/dedup.py", "add", md5_hash],
        capture_output=True,
        timeout=30,
    )

# ── main ───────────────────────────────────────────────────────────────

def main() -> int:
    if not FILE_LIST or not Path(FILE_LIST).exists():
        print(f"[ERROR] file-list not found: {FILE_LIST}", file=sys.stderr)
        return 1

    files = load_file_list(FILE_LIST)
    my_files = [p for p in files if shard_filter(p)]
    print(f"[INFO] shard {SHARD_ID}/{SHARD_TOTAL} -> {len(my_files)} files")

    out_dir = Path("batches") / "public-merged" / DATE_STR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"

    written = 0
    skipped_dup = 0
    failed_files = 0

    with open(out_path, "w", buffering=1) as out_f:
        for rel in my_files:
            tmp = Path("tmp") / rel.replace("/", "_")
            tmp.parent.mkdir(parents=True, exist_ok=True)

            if not download_file(rel, tmp):
                failed_files += 1
                continue

            try:
                if tmp.suffix == ".parquet":
                    pairs = parse_parquet(tmp)
                elif tmp.suffix == ".jsonl":
                    pairs = parse_jsonl(tmp)
                else:
                    print(f"[WARN] skip unsupported {rel}", file=sys.stderr)
                    continue

                for pair in pairs:
                    text = (pair["prompt"] + "\n\n" + pair["response"]).encode("utf-8")
                    h = md5_bytes(text)
                    if is_duplicate(h):
                        skipped_dup += 1
                        continue
                    register_md5(h)
                    out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                    written += 1
            finally:
                try:
                    tmp.unlink()
                except Exception:
                    pass

    print(f"[INFO] done: written={written} dup_skipped={skipped_dup} failed_files={failed_files
