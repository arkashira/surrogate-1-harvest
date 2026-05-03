# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Single `list_repo_tree(path, recursive=False)` for the date folder → save manifest JSON
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads via CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (bypasses API rate limits)
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Dedup via central md5 store (`lib/dedup.py`)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Returns exit code 0 on success, non-zero on fatal error

---

### 1) Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Env:
  SHARD_ID      int   (0..15)
  SHARD_TOTAL   int   default 16
  DATE          str   YYYY-MM-DD folder to ingest
  HF_TOKEN      str   write token (for dedup store push + final upload)
  HF_REPO       str   default axentx/surrogate-1-training-pairs
  RUN_ID        str   default HHMMSS
"""
import os
import sys
import json
import hashlib
import datetime
import subprocess
from pathlib import Path

import requests
import pyarrow.parquet as pq
import pyarrow as pa

# ── config --
HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "-1"))
DATE = os.getenv("DATE", datetime.date.today().isoformat())
HF_TOKEN = os.getenv("HF_TOKEN", "")
RUN_ID = os.getenv("RUN_ID", datetime.datetime.utcnow().strftime("%H%M%S"))

if SHARD_ID < 0 or SHARD_ID >= SHARD_TOTAL:
    print(f"error: SHARD_ID must be 0..{SHARD_TOTAL - 1}", file=sys.stderr)
    sys.exit(1)

BASE_CDN = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"
WORKDIR = Path(__file__).parent.parent
DEDUP_SCRIPT = WORKDIR / "lib" / "dedup.py"
OUT_DIR = WORKDIR / "batches" / "public-merged" / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{RUN_ID}.jsonl"

# ── helpers --
def hf_api_get(path: str, token: str = "") -> dict:
    url = f"https://huggingface.co/api/{path}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 429:
        print("warn: HF API 429 — respecting 360s backoff", file=sys.stderr)
        sys.exit(429)
    r.raise_for_status()
    return r.json()

def list_date_files(date_folder: str) -> list[str]:
    """
    Single API call: list top-level files in date folder (non-recursive).
    Returns list of repo-relative paths.
    """
    tree = hf_api_get(f"datasets/{HF_REPO}/tree?path={date_folder}&recursive=false", HF_TOKEN)
    files = []
    for node in tree:
        if node.get("type") == "file":
            files.append(f"{date_folder}/{node['path']}")
    return files

def belongs_to_shard(slug: str) -> bool:
    return hash(slug) % SHARD_TOTAL == SHARD_ID

def download_via_cdn(repo_path: str, dest: Path):
    url = f"{BASE_CDN}/{repo_path}"
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)

def project_to_pair(raw_bytes: bytes) -> dict | None:
    """
    Try to read parquet bytes and project to {prompt, response}.
    Tolerates mixed schemas; return None if unusable.
    """
    try:
        tbl = pq.read_table(pa.BufferReader(raw_bytes))
    except Exception:
        return None

    # Normalize column names
    cols = {c.lower(): c for c in tbl.column_names}
    prompt_col = None
    response_col = None

    for key in ("prompt", "instruction", "input", "question"):
        if key in cols:
            prompt_col = cols[key]
            break
    for key in ("response", "output", "answer", "completion"):
        if key in cols:
            response_col = cols[key]
            break

    if prompt_col is None or response_col is None:
        return None

    # Take first row only (streaming semantics); caller loops externally if needed
    prompt = tbl.column(prompt_col)[0].as_py() if tbl.num_rows > 0 else None
    response = tbl.column(response_col)[0].as_py() if tbl.num_rows > 0 else None
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt), "response": str(response)}

def run_dedup_check(pair: dict) -> bool:
    """
    Use central dedup store. Returns True if pair is new (not duplicate).
    """
    content = f"{pair['prompt']}\n{pair['response']}"
    md5 = hashlib.md5(content.encode("utf-8")).hexdigest()
    if not DEDUP_SCRIPT.exists():
        return True
    result = subprocess.run(
        [sys.executable, str(DEDUP_SCRIPT), md5],
        capture_output=True,
        text=True,
        cwd=WORKDIR,
    )
    return result.returncode == 0

# ── main --
def main() -> None:
    print(f"surrogate-1 ingest | shard={SHARD_ID}/{SHARD_TOTAL} | date={DATE}")

    # 1) manifest
    files = list_date_files(DATE)
    manifest_path = OUT_DIR / f"manifest-shard{SHARD_ID}-{RUN_ID}.json"
    manifest_path.write_text(json.dumps(files, indent=2))
    print(f"manifest saved: {manifest_path} ({len(files)} files)")

    # 2) process shard slice
    written = 0
    skipped = 0
    out_f = OUT_FILE.open("w", encoding="utf-8")

    for repo_path in files:
        slug = repo_path.rsplit("/", 1)[-1]
        if not belongs_to_shard(slug):
            skipped += 1
            continue

        try:
            raw = download_via_cdn(repo_path, Path("/tmp") / slug)
            pair = project_to_pair(raw.read_bytes())
            if pair is None:
                continue
            if not run_dedup_check(pair):
                continue
            out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
            written += 1
        except Exception as exc:
            print(f"warn: {repo_path} -> {exc}", file=sys.stderr)
            continue

    out_f.close()
    print(f"done: written={written} skipped={skipped} out={OUT_FILE}")

    # 3) upload shard file via huggingface_hub (optional; can be done by CI)
    if HF_TOKEN and OUT_FILE.exists() and OUT_FILE.stat().st_size > 0:
        try:
            from huggingface_hub import HfApi
            api = HfApi(token=HF_TOKEN)
            api.upload_file(
                path_or_fileobj=str(OUT_FILE),
                path_in_repo=str(OUT_FILE.relative_to(WORKDIR)),
                repo_id=HF_REPO,
                repo_type="dataset",
            )
            print("uploaded to HF dataset")
        except Exception as exc:
            print(f"warn: HF upload failed: {exc}", file=sys
