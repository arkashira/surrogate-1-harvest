# surrogate-1 / quality

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-first strategy**: single API call to `list_repo_tree` for the date folder → save `file-list.json` → embed in worker so Lightning training does **CDN-only fetches** with zero API calls during data load
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — no Authorization header, avoids 429 rate limits
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with deterministic shard assignment (`hash(slug) % SHARD_TOTAL`)
- Exits cleanly on 429 with 360s backoff; spreads writes across siblings if hitting 128/hr commit cap

---

### 1. Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.
Usage:
  SHARD_ID=3 SHARD_TOTAL=16 python bin/dataset-enrich.py [DATE_FOLDER]
"""
import os, sys, json, time, hashlib, datetime, subprocess, requests
from pathlib import Path
from typing import List, Dict, Any

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
HF_API = f"https://huggingface.co/api/{HF_REPO}"
HF_CDN = f"https://huggingface.co/{HF_REPO}/resolve/main"
HF_TOKEN = os.getenv("HF_TOKEN")
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER") or datetime.date.today().isoformat()

OUT_DIR = Path("batches/public-merged") / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# Central dedup store (shared via lib/dedup.py)
DEDUP_DB = Path("lib/dedup.sqlite")
DEDUP_DB.parent.mkdir(exist_ok=True)

def backoff_429(attempt: int) -> None:
    wait = 360 if attempt == 0 else (2 ** attempt) * 60
    print(f"[WARN] 429 rate limit — waiting {wait}s", file=sys.stderr)
    time.sleep(wait)

def api_get(path: str, params: Dict[str, Any] = None, retries: int = 3) -> Any:
    url = f"{HF_API}/{path.lstrip('/')}"
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 429:
                if attempt < retries:
                    backoff_429(attempt)
                    continue
                r.raise_for_status()
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt == retries:
                raise
            time.sleep(2 ** attempt)

def list_date_files(date_folder: str) -> List[str]:
    """Single API call: list files in date folder (non-recursive)."""
    items = api_get(f"tree/main/{date_folder}", params={"recursive": "false"})
    files = [it["path"] for it in items if it["type"] == "file"]
    print(f"[INFO] Found {len(files)} files in {date_folder}")
    return files

def shard_for(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def download_cdn(path: str) -> bytes:
    url = f"{HF_CDN}/{path}"
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 429:
                backoff_429(attempt)
                continue
            r.raise_for_status()
            return r.content
        except requests.exceptions.RequestException as e:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)

def parse_to_pair(content: bytes, filename: str) -> Dict[str, str]:
    """Project heterogeneous schemas to {prompt, response} only."""
    import io, pyarrow.parquet as pq, pyarrow as pa, numpy as np

    try:
        table = pq.read_table(io.BytesIO(content))
    except pa.ArrowInvalid:
        # fallback: try jsonl
        try:
            import json as _json
            lines = content.decode().strip().splitlines()
            objs = [_json.loads(l) for l in lines if l.strip()]
            # heuristic: pick first text-like fields
            prompt = objs[0].get("prompt") or objs[0].get("input") or objs[0].get("question") or ""
            response = objs[0].get("response") or objs[0].get("output") or objs[0].get("answer") or ""
            return {"prompt": str(prompt), "response": str(response)}
        except Exception:
            return {"prompt": "", "response": ""}

    cols = table.column_names
    prompt_col = next((c for c in cols if c in ("prompt", "input", "question")), None)
    response_col = next((c for c in cols if c in ("response", "output", "answer")), None)

    if prompt_col and response_col:
        p = table.column(prompt_col).to_pylist()[0]
        r = table.column(response_col).to_pylist()[0]
        return {"prompt": str(p or ""), "response": str(r or "")}

    # fallback: first two text columns
    text_cols = [c for c in cols if table.schema.field(c).type in (pa.string(), pa.large_string())]
    if len(text_cols) >= 2:
        return {"prompt": str(table.column(text_cols[0]).to_pylist()[0] or ""),
                "response": str(table.column(text_cols[1]).to_pylist()[0] or "")}
    return {"prompt": "", "response": ""}

def dedup_hash(pair: Dict[str, str]) -> str:
    blob = f"{pair['prompt']}\n---\n{pair['response']}".encode()
    return hashlib.md5(blob).hexdigest()

def is_duplicate(md5_hash: str) -> bool:
    import sqlite3
    with sqlite3.connect(str(DEDUP_DB)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY, ts REAL)")
        cur = conn.execute("SELECT 1 FROM seen WHERE md5=?", (md5_hash,))
        return cur.fetchone() is not None

def mark_seen(md5_hash: str) -> None:
    import sqlite3
    with sqlite3.connect(str(DEDUP_DB)) as conn:
        conn.execute("INSERT OR IGNORE INTO seen (md5, ts) VALUES (?, ?)", (md5_hash, time.time()))
        conn.commit()

def upload_results(lines: List[str]) -> None:
    if not lines:
        print("[INFO] No new pairs to upload")
        return

    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.rename(OUT_FILE)
    print(f"[INFO] Wrote {len(lines)} pairs to {OUT_FILE}")

    # git add + commit + push
    subprocess.run(["git", "add", str(OUT_FILE)], check=True)
    subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
    subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)

    commit_msg = f"shard{SHARD_ID}:
