# surrogate-1 / frontend

## Final Implementation (merged + hardened)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Single `list_repo_tree(path, recursive=False)` per date folder → deterministic shard assignment by hash(slug)
- Downloads assigned files via **HF CDN bypass** (`resolve/main/...`) — zero API calls during data load, avoids 429
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Dedups via content hash and writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Uploads results to the dataset repo via `huggingface_hub` (single commit per shard)
- Exits 0 on success, non-zero on hard failure (GitHub Actions will retry)

---

### 1) Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker (manifest-driven).

Environment:
  SHARD_ID        int 0..15
  SHARD_TOTAL     int (default 16)
  DATE            YYYY-MM-DD (default today UTC)
  HF_TOKEN        HuggingFace write token
  REPO_ID         dataset repo (default axentx/surrogate-1-training-pairs)
  SOURCE_PATH     repo subfolder to list (default "public")
"""
import os
import sys
import json
import hashlib
import datetime
import subprocess
import tempfile
import time
import itertools
from pathlib import Path

try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

try:
    from huggingface_hub import upload_file, hf_api
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "huggingface_hub"])
    from huggingface_hub import upload_file, hf_api

# ── config --
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SOURCE_PATH = os.getenv("SOURCE_PATH", "public").rstrip("/")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE = os.getenv("DATE", datetime.datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN", "")

if not 0 <= SHARD_ID < SHARD_TOTAL:
    print(f"Invalid SHARD_ID={SHARD_ID} for SHARD_TOTAL={SHARD_TOTAL}", file=sys.stderr)
    sys.exit(1)

# ── helpers --
def hf_api_get(path: str, params=None):
    url = f"https://huggingface.co/api/{path}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    r = requests.get(url, headers=headers, params=params, timeout=30)
    if r.status_code == 429:
        retry_after = int(r.headers.get("retry-after", 360))
        print(f"Rate limited 429, sleeping {retry_after}s", file=sys.stderr)
        time.sleep(retry_after)
        return hf_api_get(path, params)
    r.raise_for_status()
    return r.json()

def list_date_files(date_str: str):
    """Single API call: list files in SOURCE_PATH/date_str (non-recursive)."""
    folder = f"{SOURCE_PATH}/{date_str}"
    entries = hf_api_get(f"datasets/{REPO_ID}/tree", params={"path": folder, "recursive": "false"})
    files = [e for e in entries if e.get("type") == "file"]
    return files

def slug_from_path(path: str) -> str:
    """Deterministic slug for sharding: repo-relative path without extension."""
    return path.rsplit(".", 1)[0].lower().strip("/")

def shard_for_slug(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def download_via_cdn(repo_id: str, path: str, dest: Path):
    """Download via CDN (no auth/rate-limit on public datasets)."""
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)

def project_to_pair(raw_obj) -> dict:
    """
    Best-effort projection to {prompt, response} for heterogeneous schemas.
    Avoids pyarrow CastError by not assuming uniform schema.
    """
    if isinstance(raw_obj, dict):
        d = raw_obj
    else:
        try:
            d = dict(raw_obj)
        except Exception:
            return {}

    prompt_keys = {"prompt", "instruction", "input", "question", "text"}
    response_keys = {"response", "output", "answer", "completion", "result"}

    prompt = None
    response = None

    for k, v in d.items():
        kk = k.lower().strip()
        if kk in prompt_keys and prompt is None:
            prompt = v
        if kk in response_keys and response is None:
            response = v

    if prompt is None or response is None:
        items = list(d.items())
        if len(items) >= 2:
            prompt = json.dumps(dict(items[:-1]), ensure_ascii=False)
            response = items[-1][1]
        elif len(items) == 1:
            prompt = str(items[0][0])
            response = str(items[0][1])

    return {"prompt": prompt or "", "response": response or ""}

# ── main --
def main() -> int:
    from collections import defaultdict

    print(f"Starting shard {SHARD_ID}/{SHARD_TOTAL} for {DATE}", flush=True)

    files = list_date_files(DATE)
    assigned = [f for f in files if shard_for_slug(slug_from_path(f["path"])) == SHARD_ID]
    print(f"Assigned {len(assigned)} files out of {len(files)}", flush=True)

    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    out_dir = Path("batches") / "public-merged" / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"

    # Dedup store: content hash -> bool
    seen = set()
    written = 0
    skipped_dup = 0
    failed = 0

    with out_path.open("w", encoding="utf-8") as fout:
        for entry in assigned:
            rel = entry["path"]
            try:
                with tempfile.TemporaryDirectory() as td:
                    tmp_file = Path(td) / "raw"
                    download_via_cdn(REPO_ID, rel, tmp_file)

                    # Try parquet first (common), fallback to json/jsonl
                    rows = []
                    try:
                        import pyarrow.parquet as pq
                        table = pq.read_table(str(tmp_file))
                        rows = table.to_pylist()
                    except Exception:
                        text = tmp_file.read_text(encoding="utf-8")
                        text = text.strip()
                        if not text:
                            continue
                        if "\n" in text:
                            rows = [json.loads(l) for l in text.splitlines() if l.strip()]
                        else:
                            rows = [json.loads(text)]

                    for raw in rows:
                        pair = project_to_pair(raw)
                        if not pair.get("prompt") or not pair.get("response"):
                            continue

                        payload = json.dumps(pair, sort_keys=True, ensure_ascii=False)
                        md5 = hashlib.md5(payload.encode()).hexdigest()
                        if md5 in seen:
                            skipped_dup += 1
                            continue
                        seen.add(md5)

                        fout.write(payload + "\n")
                        written += 1

            except Exception as exc:
                failed += 1
                print(f"Failed {rel}: {exc}", flush=True)

    print(f"Done: written={written} skipped_dup
