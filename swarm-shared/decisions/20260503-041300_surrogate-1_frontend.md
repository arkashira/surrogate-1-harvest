# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data load and prevents mixed-schema `CastError`.

### What we change
- Keep GitHub Actions matrix (16 shards) for parallelism.
- Replace `bin/dataset-enrich.sh` heavy streaming with a lightweight Python worker that:
  - Reads a pre-computed file manifest (JSON) produced once per date folder by the Mac orchestrator (outside the 2h scope; can be reused from existing runs).
  - Downloads only its 1/16 slice via HF CDN (`resolve/main/...`) — zero API calls during data load, bypassing 429 limits.
  - Projects heterogeneous files to `{prompt, response}` only at parse time (avoids `pyarrow` schema merge/CastError).
  - Deduplicates via the existing central `lib/dedup.py` SQLite store.
  - Writes normalized JSONL to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Update workflow to install Python deps and invoke the new worker.

### Why this is safe and fast
- No changes to dataset repo layout or commit strategy (same filename pattern, same shard isolation).
- Reuses existing `lib/dedup.py` — no new infra.
- CDN downloads avoid HF API auth/rate limits entirely (per documented pattern).
- Manifest can initially be hardcoded to a small test list; full manifest integration is additive and backward-compatible.
- Entire change fits in <2h: one new Python module + workflow tweak + remove fragile shell loop.

---

## Code Changes

### 1) New worker: `bin/worker.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass shard worker for surrogate-1 public dataset ingestion.

Usage:
  python bin/worker.py \
    --shard-id 0 \
    --shard-count 16 \
    --date 2026-05-03 \
    --manifest manifest.json \
    --out-dir batches/public-merged

Environment:
  HF_TOKEN         required for upload (write to HF dataset)
  HF_REPO          dataset repo (default: axentx/surrogate-1-training-pairs)
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import requests
from huggingface_hub import HfApi

# Local dedup store (shared with HF Space)
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
CDN_BASE = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

def hash_slug(s: str) -> int:
    """Deterministic shard assignment."""
    return int(hashlib.md5(s.encode()).hexdigest(), 16)

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous file content to {prompt, response}.
    Implement per-schema adapters here to avoid mixed-schema pyarrow issues.
    """
    # Basic heuristic fallback; extend with schema-specific adapters as needed.
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def download_file_via_cdn(repo_path: str) -> bytes:
    """Download via CDN (no Authorization header -> bypasses API rate limits)."""
    url = f"{CDN_BASE}/{repo_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def parse_jsonl(content: bytes) -> List[Dict[str, Any]]:
    out = []
    for line in content.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out

def parse_parquet(content: bytes) -> List[Dict[str, Any]]:
    # Avoid loading full parquet with pyarrow if schemas vary across files.
    # Use hf_hub_download + pyarrow projection only for this file.
    import pyarrow.parquet as pq
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        f.write(content)
        f.flush()
        try:
            table = pq.read_table(f.name)
            # Select only likely text columns; avoid schema merge across files.
            cols = [c for c in table.column_names if c in {"prompt", "response", "input", "output", "question", "answer", "text"}]
            if not cols:
                # fallback: include first two string columns
                cols = [c for c, t in zip(table.column_names, table.schema.types) if str(t) in ("string", "large_string")][:2]
            df = table.select(cols).to_pandas()
            records = df.to_dict(orient="records")
            return [project_to_pair(r) for r in records]
        finally:
            os.unlink(f.name)

def worker_main(
    shard_id: int,
    shard_count: int,
    date: str,
    manifest_path: str,
    out_dir: str,
) -> None:
    api = HfApi(token=os.getenv("HF_TOKEN"))
    dedup = DedupStore()

    with open(manifest_path) as f:
        manifest = json.load(f)  # list of repo-relative paths

    # Assign shards by slug hash
    my_files: List[str] = []
    for repo_path in manifest:
        # Use repo_path as slug (or derive a stable slug)
        slug = repo_path
        if hash_slug(slug) % shard_count != shard_id:
            continue
        my_files.append(repo_path)

    os.makedirs(out_dir, exist_ok=True)
    timestamp = time.strftime("%H%M%S")
    out_path = Path(out_dir) / f"shard{shard_id}-{timestamp}.jsonl"

    written = 0
    with out_path.open("w", encoding="utf-8") as out_f:
        for repo_path in my_files:
            try:
                content = download_file_via_cdn(repo_path)
            except Exception as exc:
                print(f"[shard{shard_id}] CDN download failed {repo_path}: {exc}", file=sys.stderr)
                continue

            # Parse per extension
            if repo_path.endswith(".jsonl"):
                records = parse_jsonl(content)
            elif repo_path.endswith(".parquet"):
                records = parse_parquet(content)
            else:
                # fallback: try jsonl-like text
                try:
                    records = parse_jsonl(content)
                except Exception:
                    print(f"[shard{shard_id}] skip unknown {repo_path}", file=sys.stderr)
                    continue

            for raw in records:
                pair = project_to_pair(raw)
                if not pair["prompt"] or not pair["response"]:
                    continue

                # Deterministic md5 for dedup (same as central store)
                blob = json.dumps(pair, sort_keys=True, separators=(",", ":"))
                md5 = hashlib.md5(blob.encode()).hexdigest()
                if dedup.exists(md5):
                    continue

                dedup.add(md5)
                out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                written += 1

            # Periodic flush
            out_f.flush()

    # Upload to dataset repo (append to shard file already written to disk)
    # Use HF API only for final upload (single call per shard per run).
    if written > 0:
        api.upload_file(
            path_or_fileobj=str(out_path),
            path_in_repo=f"batches/public-merged/{date}/{out_path.name}",
            repo_id=HF_REPO,
            repo_type="dataset",
        )
        print(f"[shard{shard_id}] uploaded {written} pairs -> {out_path.name}")
    else:
        print(f"[shard{shard_id}] no new pairs to upload")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Surrogate-1 CDN-bypass shard worker")
    parser
