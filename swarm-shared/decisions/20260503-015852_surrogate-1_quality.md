# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from the GitHub Actions matrix.
- Loads a pre-generated `manifest-YYYYMMDD.json` (created once per run by a lightweight Mac/CI step) containing the list of files to process for the target date folder.
- Assigns files to shards deterministically via `hash(slug) % SHARD_TOTAL`.
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header.
- Projects each file to `{prompt, response}` only at parse time (avoids mixed-schema `pyarrow.CastError`).
- Deduplicates via the existing central md5 store (`lib/dedup.py`).
- Writes output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` (one file per shard per run).
- Exits with success/failure codes so GitHub Actions can retry cleanly.

### Steps (≤2h)

1. Create `bin/dataset-enrich.py` (replaces `bin/dataset-enrich.sh`).
2. Add `bin/gen-manifest.py` (optional helper; can also be run in CI once per cron tick).
3. Update `.github/workflows/ingest.yml` to:
   - Generate or fetch `manifest-YYYYMMDD.json` once (e.g., via a `setup` job or inline step using `gh api` + `jq`).
   - Pass manifest path to each matrix shard via env.
   - Use `bash` explicitly and ensure scripts are executable.
4. Ensure `lib/dedup.py` is importable and thread-safe for parallel shard runs.
5. Test locally with a small manifest subset.

---

## bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 \
  MANIFEST_PATH=manifest-20260503.json \
  python bin/dataset-enrich.py

Environment:
  HF_DATASET_REPO   (default: axentx/surrogate-1-training-pairs)
  HF_TOKEN          (optional; not used for CDN downloads)
  DATE_FOLDER       (e.g., 2026-05-03)
  OUT_DIR           (default: ./batches/public-merged)
"""

import json
import hashlib
import os
import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

import requests
import pyarrow.parquet as pq
import pyarrow as pa

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [shard%(shard)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

CDN_BASE = "https://huggingface.co/datasets"
DEFAULT_REPO = "axentx/surrogate-1-training-pairs"

def slug_for_path(path: str) -> str:
    """Stable slug for dedup/sharding (no extension)."""
    return path.rsplit(".", 1)[0].replace("/", "-")

def shard_assign(slug: str, total: int) -> int:
    """Deterministic shard assignment."""
    digest = hashlib.md5(slug.encode()).hexdigest()
    return int(digest, 16) % total

def cdn_download(url: str, timeout: int = 30) -> bytes:
    """Download via HF CDN (no auth)."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def project_to_pair(raw: Dict[str, Any], path: str) -> Dict[str, Any]:
    """
    Project heterogeneous file to {prompt, response} only.
    Add minimal attribution via filename pattern (no source/ts cols).
    """
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""

    # If nested (e.g., messages), attempt simple flatten
    if not prompt and isinstance(raw.get("messages"), list):
        msgs = raw["messages"]
        prompts = [m.get("content", "") for m in msgs if m.get("role") in ("user", "human")]
        responses = [m.get("content", "") for m in msgs if m.get("role") in ("assistant", "bot", "system")]
        prompt = " ".join(prompts)
        response = " ".join(responses)

    return {
        "prompt": str(prompt).strip(),
        "response": str(response).strip(),
    }

def hash_record(rec: Dict[str, Any]) -> str:
    """Stable md5 for dedup."""
    payload = f"{rec.get('prompt','')}\n{rec.get('response','')}".strip()
    return hashlib.md5(payload.encode()).hexdigest()

def process_parquet(content: bytes, path: str) -> List[Dict[str, Any]]:
    """Read parquet bytes and project rows."""
    try:
        table = pq.read_table(pa.BufferReader(content))
    except Exception as exc:
        logging.warning("Failed to read parquet %s: %s", path, exc)
        return []

    rows = table.to_pylist()
    out = []
    for row in rows:
        try:
            pair = project_to_pair(row, path)
            if pair["prompt"] and pair["response"]:
                pair["_md5"] = hash_record(pair)
                pair["_source_path"] = path
                out.append(pair)
        except Exception as exc:
            logging.debug("Skipping malformed row in %s: %s", path, exc)
    return out

def process_jsonl(content: bytes, path: str) -> List[Dict[str, Any]]:
    out = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            pair = project_to_pair(row, path)
            if pair["prompt"] and pair["response"]:
                pair["_md5"] = hash_record(pair)
                pair["_source_path"] = path
                out.append(pair)
        except Exception as exc:
            logging.debug("Skipping malformed line in %s: %s", path, exc)
    return out

def process_file(path: str, dedup: DedupStore) -> int:
    """Download + project + dedup; return accepted count."""
    repo = os.getenv("HF_DATASET_REPO", DEFAULT_REPO)
    url = f"{CDN_BASE}/{repo}/resolve/main/{path}"
    ext = path.rsplit(".", 1)[-1].lower()

    try:
        raw = cdn_download(url, timeout=30)
    except Exception as exc:
        logging.warning("CDN download failed %s: %s", path, exc)
        return 0

    try:
        if ext == "parquet":
            rows = process_parquet(raw, path)
        elif ext == "jsonl":
            rows = process_jsonl(raw, path)
        elif ext == "json":
            # treat as single json array or line-delimited
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    rows_raw = data
                else:
                    rows_raw = [data]
            except Exception:
                rows_raw = [json.loads(l) for l in raw.decode().splitlines() if l.strip()]
            rows = []
            for row in rows_raw:
                pair = project_to_pair(row, path)
                if pair["prompt"] and pair["response"]:
                    pair["_md5"] = hash_record(pair)
                    pair["_source_path"] = path
                    rows.append(pair)
        else:
            logging.warning("Unsupported extension %s for %s", ext, path)
            return 0
    except Exception as exc:
        logging.warning("Failed to parse %s: %s", path, exc)
       
