# surrogate-1 / quality

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** that:

- Uses a **single `list_repo_tree` snapshot** (JSON manifest) generated once per date on the Mac orchestrator and committed to the repo (or passed via env) — avoids recursive `list_repo_files` and rate limits.
- Downloads **only assigned shard files via HF CDN** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with **no Authorization header** — bypasses `/api/` rate limits entirely.
- Projects heterogeneous schemas to `{prompt, response}` at parse time; writes `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.
- Central dedup via existing `lib/dedup.py` (SQLite) — cross-run dedup handled by HF Space; this worker accepts occasional duplicates to avoid state sync complexity.
- Runs in GitHub Actions matrix (`SHARD_ID=0..15`) with deterministic hash-bucket assignment (`hash(slug) % 16`).
- Mac orchestrator only: generates manifest, commits, triggers workflow.

---

### File changes

#### 1) `bin/dataset-enrich.py` (new)
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass worker for surrogate-1 public dataset ingestion.

Usage:
  SHARD_ID=0 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --manifest manifests/2026-05-03.json \
    --date 2026-05-03

Environment:
  HF_TOKEN         Optional for CDN downloads (not required for public CDN).
  SHARD_ID         0..15 (required in CI matrix)
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

try:
    from lib.dedup import DedupStore
except ImportError:
    # Fallback for direct execution outside package layout
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from lib.dedup import DedupStore

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
BATCH_DIR = Path("batches/public-merged")
CHUNK_SIZE = 8 * 1024 * 1024  # 8MB


def shard_for_slug(slug: str, n_shards: int = 16) -> int:
    """Deterministic shard assignment."""
    digest = hashlib.md5(slug.encode()).hexdigest()
    return int(digest, 16) % n_shards


def download_cdn(path: str, repo: str, timeout: int = 30, retries: int = 3) -> bytes:
    url = HF_CDN_TEMPLATE.format(repo=repo, path=path)
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            resp.raise_for_status()
            content = b""
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                content += chunk
            return content
        except Exception as exc:
            if attempt == retries:
                raise
            sleep_sec = 2 ** attempt
            print(f"[WARN] CDN download failed ({exc}), retry {attempt}/{retries} in {sleep_sec}s: {url}", file=sys.stderr)
            time.sleep(sleep_sec)


def extract_pair_from_parquet(content: bytes) -> list[dict]:
    table = pq.read_table(pa.BufferReader(content))
    rows = []
    for col in table.columns:
        name = col.name.lower()
        if name == "prompt" or "prompt" in name:
            prompt_col = col
        elif name == "response" or "response" in name or "completion" in name or "output" in name:
            response_col = col
    # If not found, try first two text columns
    if "prompt_col" not in locals() or "response_col" not in locals():
        text_cols = [c for c in table.columns if pa.types.is_string(c.type) or pa.types.is_large_string(c.type)]
        if len(text_cols) >= 2:
            prompt_col, response_col = text_cols[0], text_cols[1]
        elif len(text_cols) == 1:
            prompt_col, response_col = text_cols[0], text_cols[0]
        else:
            # fallback: first two columns
            prompt_col, response_col = table.columns[0], table.columns[1]

    for i in range(table.num_rows):
        prompt = str(prompt_col[i].as_py()) if i < len(prompt_col) else ""
        response = str(response_col[i].as_py()) if i < len(response_col) else ""
        if prompt.strip() or response.strip():
            rows.append({"prompt": prompt.strip(), "response": response.strip()})
    return rows


def extract_pair_from_jsonl(content: bytes) -> list[dict]:
    rows = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
        response = obj.get("response") or obj.get("output") or obj.get("answer") or obj.get("completion") or ""
        if isinstance(prompt, str) and isinstance(response, str):
            if prompt.strip() or response.strip():
                rows.append({"prompt": prompt.strip(), "response": response.strip()})
        elif isinstance(prompt, (list, dict)) or isinstance(response, (list, dict)):
            rows.append({"prompt": json.dumps(prompt), "response": json.dumps(response)})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="CDN-bypass shard worker")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD for output folder")
    parser.add_argument("--n-shards", type=int, default=16)
    parser.add_argument("--dedup-db", default="dedup.sqlite")
    args = parser.parse_args()

    shard_id = int(os.environ.get("SHARD_ID", -1))
    if shard_id < 0 or shard_id >= args.n_shards:
        print(f"[ERROR] SHARD_ID must be 0..{args.n_shards-1}", file=sys.stderr)
        sys.exit(1)

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"[ERROR] Manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    with manifest_path.open() as f:
        manifest = json.load(f)

    # manifest format: { "date": "YYYY-MM-DD", "files": ["path1", "path2", ...] }
    files = manifest.get("files", [])
    if not files:
        print("[INFO] No files in manifest; nothing to do.")
        sys.exit(0)

    my_files = [p for p in files if shard_for_slug(p, args.n_shards) == shard_id]
    print(f"[INFO] Shard {shard_id}: processing {len(my_files)} files out of {len(files)}")

    dedup = DedupStore(args.dedup_db)
    os.makedirs(BATCH_DIR / args.date, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    out_path = BATCH_DIR / args.date / f"shard{shard_id}-{timestamp}.jsonl"

    accepted = 0
    duplicates = 0
    failed = 0

    with out_path.open("w", buffering=1) as out_f:
        for path in tqdm(my_files, desc=f"Shard {shard_id}"):
            try:
                content = download_cdn(path, args.repo)
                if path.endswith(".parquet"):
                    pairs = extract_pair_from_parquet(content)
                elif path.endswith(".jsonl"):
                    pairs = extract_pair_from_jsonl(content)
                else:
                    print(f"[WARN] Skipping unsupported file: {path}", file=sys.stderr)
                    continue
