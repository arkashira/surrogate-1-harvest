# surrogate-1 / quality

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single `list_repo_tree` call** (per date folder) to enumerate files once, saves manifest JSON, then performs **CDN-only fetches** (`https://huggingface.co/datasets/.../resolve/main/...`) to bypass HF API rate limits during data loading.
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids `pyarrow.CastError`).
- Deduplicates via central `lib/dedup.py` md5 store.
- Writes output to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with deterministic shard assignment via `hash(slug) % SHARD_TOTAL`.
- Reuses existing running HF Space when possible (saves Lightning quota) and respects idle-stop by checking status before `.run()`.

---

### Steps (1h 30m total)

1. **Create `bin/dataset-enrich.py`** (45m)  
   - Shebang `#!/usr/bin/env python3`, executable `chmod +x`.
   - CLI: `--shard-id`, `--shard-total=16`, `--date-folder`, `--repo=axentx/surrogate-1-training-pairs`, `--output-repo=axentx/surrogate-1-training-pairs`.
   - Logic:
     - `list_repo_tree(path=date_folder, recursive=False)` → manifest JSON saved locally.
     - For each file in shard slice: `hf_hub_download` OR direct CDN fetch (no auth) → parse with schema projection → yield `{prompt, response, _source_file, _sha256}`.
     - Dedup via `lib/dedup.py` (md5 of normalized content).
     - Stream-write shard output lines.
   - Upload via `huggingface_hub` commit to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

2. **Update `.github/workflows/ingest.yml`** (15m)  
   - Change matrix runner to invoke `python3 bin/dataset-enrich.py` with env vars.
   - Keep 16-shard matrix, pass `SHARD_ID`, `SHARD_TOTAL`, `DATE_FOLDER`.

3. **Add `requirements.txt` updates** (5m)  
   - Ensure `huggingface_hub`, `datasets`, `pyarrow`, `requests`, `tqdm`.

4. **Smoke test locally** (15m)  
   - Run with `SHARD_ID=0 SHARD_TOTAL=2` on small date folder, verify output JSONL and dedup behavior.

5. **Commit & push** (10m)  
   - Tag: `#surrogate-1 #quality #cdn-bypass`.

---

### Code Snippets

#### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python3 bin/dataset-enrich.py \
    --date-folder 2026-05-03 \
    --repo axentx/surrogate-1-training-pairs
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download, login
from tqdm import tqdm

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa: E402

HF_API = HfApi()
CDN_BASE = "https://huggingface.co/datasets"

def parse_args():
    parser = argparse.ArgumentParser(description="Shard worker: ingest & normalize")
    parser.add_argument("--shard-id", type=int, default=int(os.getenv("SHARD_ID", 0)))
    parser.add_argument("--shard-total", type=int, default=int(os.getenv("SHARD_TOTAL", 16)))
    parser.add_argument("--date-folder", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--output-repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--hf-token", default=os.getenv("HF_TOKEN"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()

def build_manifest(api: HfApi, repo: str, date_folder: str) -> List[str]:
    """Single API call: list files in date folder (non-recursive)."""
    try:
        tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    except Exception as e:
        # Fallback: try repo root if folder not found
        tree = api.list_repo_tree(repo=repo, path="", recursive=False)
        tree = [f for f in tree if f.startswith(f"{date_folder}/")]
    # Return full repo paths
    return [f.r["path"] if isinstance(f, dict) else f.path if hasattr(f, "path") else str(f) for f in tree]

def slug_for_path(path: str) -> str:
    """Deterministic slug from path for shard assignment."""
    return path.strip("/").replace("/", "--")

def shard_for_path(path: str, shard_total: int) -> int:
    return hash(slug_for_path(path)) % shard_total

def cdn_url(repo: str, path: str) -> str:
    return f"{CDN_BASE}/{repo}/resolve/main/{path}"

def project_to_pair(file_path: str, raw_bytes: bytes) -> Optional[Dict]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Supports: jsonl, json, parquet (via streaming read), text.
    """
    import io
    # Try JSON/JSONL first
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Possibly parquet/binary; attempt parquet projection via pyarrow if available
        try:
            import pyarrow.parquet as pq
            table = pq.read_table(io.BytesIO(raw_bytes), columns=["prompt", "response"])
            if table.num_rows == 0:
                return None
            row = table.slice(0, 1).to_pydict()
            prompt = row.get("prompt", [""])[0]
            response = row.get("response", [""])[0]
            if not prompt and not response:
                return None
            return {"prompt": str(prompt), "response": str(response), "_source_file": file_path}
        except Exception:
            return None

    # JSONL: take first valid line
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if prompt is None and response is None:
                # Maybe whole object is prompt, use placeholder
                continue
            return {"prompt": str(prompt or ""), "response": str(response or ""), "_source_file": file_path}
        except json.JSONDecodeError:
            continue

    # Plain text: treat first non-empty line as prompt, rest as response (heuristic)
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    if len(lines) == 1:
        return {"prompt": lines[0], "response": "", "_source_file": file_path}
    return {"prompt": lines[0], "response": "\n".join(lines[1:]), "_source_file": file_path}

def fetch_via_cdn(url: str, timeout: int = 30) -> Optional[bytes]:
    """CDN fetch without Authorization header (bypasses API rate limits)."""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "axentx-surrogate-ingest"})
       
