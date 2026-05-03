# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list saved to `manifest.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (avoids 429 API limits)
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Dedups via central `lib/dedup.py` md5 store
- Uploads output to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Reuses existing Lightning Studio pattern: list running studios before creating new ones (saves quota)
- Uses `SHELL=/bin/bash` in any cron/workflow wrappers (prevents exec errors)

---

## Code Changes

### 1. New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py
"""
import os, sys, json, hashlib, time, datetime, subprocess, tempfile
from pathlib import Path

import requests
import pyarrow.parquet as pq
import pyarrow as pa

REPO_OWNER = "axentx"
REPO_NAME  = "surrogate-1-training-pairs"
BASE_CDN   = f"https://huggingface.co/datasets/{REPO_OWNER}/{REPO_NAME}/resolve/main"
API_ROOT   = f"https://huggingface.co/api/datasets/{REPO_OWNER}/{REPO_NAME}"

# Deterministic shard assignment
def shard_for(slug: str, total: int) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % total

def list_date_files(date: str, token: str):
    """Single API call: list top-level files for DATE folder."""
    url = f"{API_ROOT}/tree/{date}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 429:
        wait = int(r.headers.get("Retry-After", 360))
        print(f"Rate limited. Waiting {wait}s", file=sys.stderr)
        time.sleep(wait)
        return list_date_files(date, token)
    r.raise_for_status()
    entries = r.json()
    # Keep only files (not dirs) and parquet/jsonl
    files = [e["path"] for e in entries if e.get("type") == "file"]
    return files

def cdn_download(path: str, dest: Path):
    """Download via CDN (no auth)."""
    url = f"{BASE_CDN}/{path}"
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def project_to_pair(local_path: Path):
    """Project file to {prompt, response} regardless of schema."""
    try:
        # Try parquet first
        tbl = pq.read_table(local_path)
    except Exception:
        # Fallback: try jsonl
        try:
            with open(local_path) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            tbl = pa.Table.from_pylist(lines)
        except Exception:
            return []

    rows = []
    cols = set(tbl.column_names)
    # Heuristic field names
    prompt_col = next((c for c in ("prompt", "input", "question", "instruction") if c in cols), None)
    response_col = next((c for c in ("response", "output", "answer", "completion") if c in cols), None)

    if prompt_col and response_col:
        for i in range(tbl.num_rows):
            pr = tbl.column(prompt_col)[i].as_py()
            resp = tbl.column(response_col)[i].as_py()
            if isinstance(pr, str) and isinstance(resp, str) and pr.strip() and resp.strip():
                rows.append({"prompt": pr.strip(), "response": resp.strip()})
    return rows

def upload_shard(output_path: Path, date: str, shard_id: int, token: str):
    """Upload JSONL to HF dataset repo."""
    remote_path = f"batches/public-merged/{date}/shard{shard_id}-{datetime.datetime.utcnow().strftime('%H%M%S')}.jsonl"
    # Use huggingface_hub for atomic commit
    from huggingface_hub import upload_file
    upload_file(
        path_or_fileobj=str(output_path),
        path_in_repo=remote_path,
        repo_id=f"{REPO_OWNER}/{REPO_NAME}",
        token=token,
    )
    print(f"Uploaded {remote_path}")

def main():
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date = os.getenv("DATE", datetime.date.today().isoformat())
    token = os.getenv("HF_TOKEN", "")

    print(f"Shard {shard_id}/{shard_total} | DATE={date}")

    # 1) List files once
    files = list_date_files(date, token)
    manifest_path = Path("manifest.json")
    manifest_path.write_text(json.dumps({"date": date, "files": files}, indent=2))
    print(f"Manifest saved: {len(files)} files")

    # 2) Determine assigned files
    assigned = [f for f in files if shard_for(f, shard_total) == shard_id]
    print(f"Assigned {len(assigned)} files")

    # 3) Process
    out_path = Path(f"shard{shard_id}.jsonl")
    seen = set()
    # Import central dedup
    sys.path.insert(0, str(Path(__file__).parent))
    from lib.dedup import is_duplicate, mark_seen

    with out_path.open("w", buffering=1) as out_f:
        for i, fpath in enumerate(assigned):
            with tempfile.TemporaryDirectory() as td:
                local = Path(td) / Path(fpath).name
                try:
                    cdn_download(fpath, local)
                    pairs = project_to_pair(local)
                except Exception as e:
                    print(f"Error processing {fpath}: {e}", file=sys.stderr)
                    continue

                for p in pairs:
                    # Dedup by content hash
                    h = hashlib.md5(json.dumps(p, sort_keys=True).encode()).hexdigest()
                    if is_duplicate(h):
                        continue
                    mark_seen(h)
                    out_f.write(json.dumps(p, ensure_ascii=False) + "\n")
            if (i + 1) % 10 == 0:
                print(f"  processed {i+1}/{len(assigned)}")

    # 4) Upload
    if out_path.stat().st_size > 0:
        upload_shard(out_path, date, shard_id, token)
    else:
        print("No new pairs to upload.")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/dataset-enrich.py
```

---

### 2. Update GitHub Actions workflow: `.github/workflows/ingest.yml`

Ensure `SHELL=/bin/bash` and invoke via `python` (or `bash` if kept as `.sh` wrapper). Replace step with:

```yaml
jobs:
  ingest:
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    env:
      SHARD_ID: ${{ matrix
