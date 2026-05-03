# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single pre-listed manifest** (`manifest/<DATE_FOLDER>.json`) produced by the Mac orchestrator (or on first miss via one rate-limited `list_repo_tree` call) to avoid recursive `list_repo_files` and HF API 429/128-commit limits.
- Downloads only assigned shard files via **HF CDN direct URLs** (`https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/...`) — zero Authorization header, bypasses `/api/` rate limits.
- Projects each file to `{prompt, response}` at parse time (handles mixed schemas per surrogate-1 training pattern) and streams output as newline JSON.
- Writes deterministic output to `batches/public-merged/<DATE_FOLDER>/shard<SHARD_ID>-<HHMMSS>.jsonl` and commits via HF API (one commit per shard per run) respecting 128/hr/repo cap.
- Keeps `lib/dedup.py` as the cross-run source-of-truth SQLite store (central md5 dedup) but does not require it to be resident in the runner — runners may produce duplicates that the HF Space later dedups (accepted trade-off).
- Adds proper Bash shebang and executable bit for any wrapper; GitHub Actions matrix remains unchanged.

---

## Concrete Steps (timed)

1. **Create `bin/manifest.py`** (10 min) — one-shot tool to produce `manifest/<DATE_FOLDER>.json` from `list_repo_tree` (non-recursive per folder) to avoid 429/1000 req limit.
2. **Replace `bin/dataset-enrich.sh` with `bin/dataset-enrich.py`** (60 min) — implement shard assignment, CDN fetch, schema projection, streaming JSONL output.
3. **Update GitHub Actions matrix** (5 min) — ensure env vars `SHARD_ID`, `SHARD_TOTAL`, `DATE_FOLDER` passed; install Python deps.
4. **Add `requirements.txt` updates** (5 min) — include `requests`, keep `datasets`, `huggingface_hub`, `pyarrow`, `numpy`.
5. **Test locally** (20 min) — run one shard against a small date folder; verify output schema and CDN fetch.
6. **Commit and push** (10 min) — ensure executable bits and shebangs correct.

Total: ~1h 50m.

---

## Code Snippets

### `bin/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest/<DATE_FOLDER>.json listing all files under that folder
(non-recursive tree walk) to avoid HF API list_repo_files recursion/429.
Run from Mac orchestrator after rate-limit window or on first miss.
"""
import os
import json
from datetime import datetime, timezone
from huggingface_hub import HfApi

REPO = "datasets/axentx/surrogate-1-training-pairs"
OUT_DIR = "manifest"

def main(date_folder: str | None = None):
    if date_folder is None:
        date_folder = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(OUT_DIR, exist_ok=True)
    api = HfApi()
    # Single call per folder; avoid recursive=True on big repos
    tree = api.list_repo_tree(repo_id=REPO, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if item.type == "file"]
    # If folders exist, we could recurse one level only as needed.
    manifest_path = os.path.join(OUT_DIR, f"{date_folder}.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"date_folder": date_folder, "files": sorted(files)}, f, indent=2)
    print(f"Wrote {len(files)} files to {manifest_path}")

if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else None)
```

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE_FOLDER=2026-05-03 python bin/dataset-enrich.py
"""
import os
import sys
import json
import hashlib
import datetime
import requests
from pathlib import Path

HF_DATASET = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main"

SHARD_ID = int(os.environ.get("SHARD_ID", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "16"))
DATE_FOLDER = os.environ.get("DATE_FOLDER", datetime.datetime.utcnow().strftime("%Y-%m-%d"))

MANIFEST_PATH = Path(__file__).parent.parent / "manifest" / f"{DATE_FOLDER}.json"
OUT_DIR = Path(__file__).parent.parent / "batches" / "public-merged" / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)

def deterministic_shard(key: str, total: int) -> int:
    return int(hashlib.md5(key.encode()).hexdigest(), 16) % total

def project_to_pair(obj: dict) -> dict | None:
    """
    Surrogate-1 pattern: project heterogeneous files to {prompt, response}
    at parse time. Keep minimal, drop source/ts cols.
    """
    # Common patterns seen in training pairs
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or obj.get("instruction")
    response = obj.get("response") or obj.get("output") or obj.get("answer") or obj.get("completion")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt), "response": str(response)}

def cdn_url(path: str) -> str:
    return f"{BASE_CDN}/{path.lstrip('/')}"

def load_manifest() -> list[str]:
    if not MANIFEST_PATH.exists():
        print(f"Manifest missing: {MANIFEST_PATH}. Run bin/manifest.py first.", file=sys.stderr)
        sys.exit(1)
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("files", [])

def stream_shard_files():
    files = load_manifest()
    assigned = [f for f in files if deterministic_shard(f, SHARD_TOTAL) == SHARD_ID]
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} assigned {len(assigned)} files")

    timestamp = datetime.datetime.utcnow().strftime("%H%M%S")
    out_path = OUT_DIR / f"shard{SHARD_ID}-{timestamp}.jsonl"
    written = 0

    with open(out_path, "w", encoding="utf-8") as out_f:
        for rel in assigned:
            url = cdn_url(rel)
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
            except Exception as exc:
                print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
                continue

            # Try parquet or jsonl detection by extension/content
            lower = rel.lower()
            if lower.endswith(".parquet"):
                import pyarrow.parquet as pq
                import io
                try:
                    table = pq.read_table(io.BytesIO(resp.content))
                    for batch in table.to_batches(max_chunksize=1000):
                        for row in batch.to_pylist():
                            pair = project_to_pair(row)
                            if pair:
                                out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                                written += 1
                except Exception as exc:
                    print(f"Parquet decode failed for {rel}: {exc}", file=sys.stderr)
                    continue
            elif lower.endswith(".jsonl"):
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                   
