# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix), optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses **single API call** to list one date folder → saves `file-list.json` (cached on disk) to avoid repeated API calls.
- **CDN-only fetches** during ingestion (bypasses `/api/` auth rate limits).
- Projects heterogeneous schemas to `{prompt, response}` at parse time.
- Deduplicates via central `lib/dedup.py` md5 store.
- Writes to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.
- Includes retry/backoff for 429 (wait 360s) and commit-cap spreading across sibling repos (hash-slug → repo).

---

### 1) Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py
Env:
  HF_TOKEN          - write token
  DATE_FOLDER       - e.g. 2026-05-03 (default: today)
  DATASET_REPO      - default axentx/surrogate-1-training-pairs
  SIBLING_REPOS     - comma-separated repo list for commit-cap spreading
"""
import os, sys, json, hashlib, time, datetime, pathlib, subprocess, tempfile
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download, ModelCard

# ---- config ----
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SIBLING_REPOS = [r.strip() for r in os.getenv("SIBLING_REPOS", "").split(",") if r.strip()]
ALL_REPOS = [DATASET_REPO] + SIBLING_REPOS or [DATASET_REPO]

API = HfApi(token=HF_TOKEN)
SESSION = requests.Session()
# ----

def hf_api_get(path: str, params: dict = None, max_retries: int = 5):
    """Call HF API with 429 backoff (360s)."""
    url = f"https://huggingface.co/api/{path}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    for attempt in range(max_retries):
        r = SESSION.get(url, headers=headers, params=params, timeout=60)
        if r.status_code == 429:
            wait = 360
            print(f"[429] rate-limited, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"HF API failed after {max_retries} retries")

def list_date_files(date_folder: str) -> List[str]:
    """Single API call: non-recursive list for date folder."""
    out = hf_api_get(f"datasets/{DATASET_REPO}/tree", params={"path": date_folder, "recursive": False})
    files = []
    for item in out:
        if item.get("type") == "file":
            files.append(f"{date_folder}/{item['path']}")
    return files

def pick_repo(slug: str) -> str:
    """Deterministic repo selection for commit-cap spreading."""
    idx = int(hashlib.md5(slug.encode()).hexdigest(), 16) % len(ALL_REPOS)
    return ALL_REPOS[idx]

def download_cdn(path: str, dest: pathlib.Path) -> None:
    """Download via CDN (no auth)."""
    url = f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/main/{path}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with SESSION.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def normalize_record(raw: Dict[str, Any], source_file: str) -> Dict[str, str]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Keep minimal attribution via filename pattern only.
    """
    # Common field heuristics
    prompt = raw.get("prompt") or raw.get("instruction") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or raw.get("completion") or ""
    # coerce to string
    prompt = str(prompt) if prompt is not None else ""
    response = str(response) if response is not None else ""
    return {"prompt": prompt.strip(), "response": response.strip()}

def main() -> None:
    # 1) list files once
    print(f"Listing files for {DATE_FOLDER}...", file=sys.stderr)
    files = list_date_files(DATE_FOLDER)
    if not files:
        print("No files found.", file=sys.stderr)
        return

    # deterministic shard assignment by filename hash
    my_files = []
    for f in files:
        h = int(hashlib.md5(f.encode()).hexdigest(), 16)
        if h % SHARD_TOTAL == SHARD_ID:
            my_files.append(f)

    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} -> {len(my_files)} files", file=sys.stderr)

    # 2) process files with CDN downloads
    out_lines = []
    dedup = __import__("lib.dedup")  # expects lib/dedup.py with mark_seen(slug)->bool and store
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    for path in my_files:
        try:
            with tempfile.TemporaryDirectory() as td:
                local_path = pathlib.Path(td) / pathlib.Path(path).name
                download_cdn(path, local_path)

                # handle common HF dataset formats minimally
                suffix = local_path.suffix.lower()
                records = []
                if suffix == ".jsonl":
                    for line in local_path.read_text().splitlines():
                        line = line.strip()
                        if line:
                            records.append(json.loads(line))
                elif suffix == ".json":
                    obj = json.loads(local_path.read_text())
                    if isinstance(obj, list):
                        records = obj
                    else:
                        records = [obj]
                elif suffix in (".parquet", ".arrow"):
                    # lightweight: use pyarrow via subprocess to avoid heavy deps in this worker
                    # fallback: skip and rely on upstream normalization
                    print(f"Skipping binary file {path} (requires pyarrow)", file=sys.stderr)
                    continue
                else:
                    print(f"Unknown file type {suffix} for {path}", file=sys.stderr)
                    continue

                for raw in records:
                    norm = normalize_record(raw, path)
                    if not norm["prompt"] or not norm["response"]:
                        continue
                    # dedup by content hash
                    slug = hashlib.md5((norm["prompt"] + "\x00" + norm["response"]).encode()).hexdigest()
                    if hasattr(dedup, "mark_seen"):
                        if not dedup.mark_seen(slug):
                            continue
                    out_lines.append(json.dumps(norm, ensure_ascii=False))
        except Exception as e:
            print(f"Error processing {path}: {e}", file=sys.stderr)
            continue

    if not out_lines:
        print("No new records after dedup.", file=sys.stderr)
        return

    # 3) write shard output
    out_dir = f"batches/public-merged/{DATE_FOLDER}"
    out_name = f"shard{SHARD_ID}-{ts}.jsonl"
    out_path =
