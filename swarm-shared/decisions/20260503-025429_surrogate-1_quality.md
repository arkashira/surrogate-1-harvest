# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list → shard by `hash(slug) % SHARD_TOTAL`
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with zero Authorization header during data fetch
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with no extra metadata columns
- Shebang `#!/usr/bin/env bash` wrapper for cron safety, executable bit, `SHELL=/bin/bash` in crontab

---

## Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset shards.

Environment:
  SHARD_ID      (int) 0..15
  SHARD_TOTAL   (int) default 16
  DATE          (str) YYYY-MM-DD folder to process
  HF_TOKEN      (str) write token for axentx/surrogate-1-training-pairs
  HF_REPO       (str) default axentx/surrogate-1-training-pairs
  DEDUP_DB_URL  (str) optional; passed to lib.dedup
"""

import os
import sys
import json
import hashlib
import datetime
from pathlib import Path
from typing import Dict, Any, List

import requests
import pyarrow as pa
import pyarrow.parquet as pq
from huggingface_hub import HfApi

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa: E402

HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.date.today().isoformat())
HF_TOKEN = os.getenv("HF_TOKEN")
DEDUP_DB_URL = os.getenv("DEDUP_DB_URL")

if not HF_TOKEN:
    print("HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

api = HfApi(token=HF_TOKEN)
dedup = DedupStore(DEDUP_DB_URL)

# ----------------------------
# 1) Manifest: list files once
# ----------------------------
def list_date_files(date_folder: str) -> List[str]:
    """
    Single API call: list top-level files in date folder (non-recursive).
    Avoids recursive pagination and rate-limit churn.
    """
    try:
        tree = api.list_repo_tree(
            repo_id=HF_REPO,
            path=date_folder,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as exc:
        print(f"Failed to list repo tree for {date_folder}: {exc}", file=sys.stderr)
        return []

    # tree can be list or object depending on huggingface_hub version
    items = tree if isinstance(tree, list) else getattr(tree, "items", [])
    files = []
    for item in items:
        if hasattr(item, "path"):
            files.append(item.path)
        elif isinstance(item, dict):
            files.append(item.get("path", ""))
    return [f for f in files if f]

# ----------------------------
# 2) Shard assignment
# ----------------------------
def shard_for_slug(slug: str) -> int:
    """Deterministic shard by slug hash."""
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()
    return int(digest, 16) % SHARD_TOTAL

# ----------------------------
# 3) CDN-bypass download helpers
# ----------------------------
def cdn_download(repo: str, path: str) -> bytes:
    """
    Download via CDN URL (no Authorization header).
    Public dataset files are accessible without token.
    """
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def safe_parquet_to_pairs(content: bytes) -> List[Dict[str, str]]:
    """
    Load parquet bytes and project to {prompt, response}.
    Avoids load_dataset(streaming=True) on heterogeneous schemas.
    """
    table = pq.read_table(io.BytesIO(content))
    pairs = []
    cols = set(table.column_names)

    # Heuristic projection: accept common field names
    prompt_col = next((c for c in ("prompt", "instruction", "input", "question") if c in cols), None)
    response_col = next((c for c in ("response", "output", "answer", "completion") if c in cols), None)

    if prompt_col is None or response_col is None:
        # If no known columns, try first text-like column pair
        text_cols = [c for c in cols if table.schema.field(c).type in (pa.string(), pa.large_string())]
        if len(text_cols) >= 2:
            prompt_col, response_col = text_cols[0], text_cols[1]
        else:
            return []

    col_prompt = table.column(prompt_col)
    col_response = table.column(response_col)

    for i in range(table.num_rows):
        p = col_prompt[i].as_py()
        r = col_response[i].as_py()
        if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
            pairs.append({"prompt": p.strip(), "response": r.strip()})
    return pairs

# ----------------------------
# 4) Worker main
# ----------------------------
def run() -> None:
    files = list_date_files(DATE)
    if not files:
        print(f"No files found for date={DATE}", file=sys.stderr)
        return

    assigned_files = [f for f in files if shard_for_slug(f) == SHARD_ID]
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} processing {len(assigned_files)} files from {len(files)} total")

    out_dir = Path("batches") / "public-merged" / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"

    written = 0
    skipped_dup = 0
    errors = 0

    with out_path.open("w", encoding="utf-8") as fout:
        for rel_path in assigned_files:
            try:
                content = cdn_download(HF_REPO, rel_path)
                pairs = safe_parquet_to_pairs(content)

                for pair in pairs:
                    # Dedup by content hash
                    payload = f"{pair['prompt']}\n{pair['response']}"
                    md5 = hashlib.md5(payload.encode("utf-8")).hexdigest()
                    if dedup.exists(md5):
                        skipped_dup += 1
                        continue

                    record = {"prompt": pair["prompt"], "response": pair["response"]}
                    fout.write(json.dumps(record, ensure_ascii=False) + "\n")
                    dedup.add(md5)
                    written += 1

            except Exception as exc:
                errors += 1
                print(f"Error processing {rel_path}: {exc}", file=sys.stderr)

    print(f"Shard {SHARD_ID} done: written={written}, dup_skipped={skipped_dup}, errors={errors}, out={out_path}")

    # Upload shard file to dataset repo (single commit per shard run)
    if written > 0:
        try:
            api.upload_file(
                path_or_fileobj=str(out_path),
                path_in_repo=str(out_path.relative_to(Path.cwd())),
                repo_id=HF_REPO,
                repo_type="dataset",
            )
            print(f"Uploaded {out_path} to {HF_REPO}")
        except Exception as exc:
