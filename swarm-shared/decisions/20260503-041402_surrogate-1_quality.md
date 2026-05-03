# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/worker.py`** — manifest-driven, CDN-only fetcher that:
   - Accepts `SHARD_ID` and `TOTAL_SHARDS` (16) from the matrix
   - Calls HF API **once** (after rate-limit window) to list a single date folder via `list_repo_tree(..., recursive=False)` and writes `manifest.json`
   - Embeds that manifest in the worker; training later uses **zero API calls** (CDN-only via `https://huggingface.co/datasets/.../resolve/main/...`)
   - Projects every file to `{prompt, response}` at parse time (avoids `load_dataset(streaming=True)` on heterogeneous repos)
   - Dedups via existing `lib/dedup.py` central md5 store
   - Outputs `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`

2. **Update `bin/dataset-enrich.sh`** — thin wrapper that:
   - Sets `SHELL=/bin/bash`
   - Validates `HF_TOKEN` and `SHARD_ID`
   - Invokes `python3 bin/worker.py` with proper args
   - `chmod +x` preserved

3. **Update `.github/workflows/ingest.yml`** — ensure matrix uses `python3` and passes `SHARD_ID`/`TOTAL_SHARDS`; no other logic in shell.

4. **Add `requirements.txt` entries** if missing: `requests`, `tqdm` (lightweight).

---

## Code Snippets

### `bin/worker.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

- SHARD_ID and TOTAL_SHARDS (16) determine deterministic slice.
- Single HF API list_repo_tree call -> manifest.json
- All data fetches are CDN-only (no Authorization header).
- Projects heterogeneous files to {prompt, response} at parse time.
- Dedup via lib.dedup central md5 store.
"""
import os
import sys
import json
import hashlib
import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
API = HfApi()
CDN_ROOT = f"https://huggingface.co/{HF_REPO}/resolve/main"

# Deterministic sharding
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}
DATE_TAG = datetime.datetime.utcnow().strftime("%Y-%m-%d")
OUT_DIR = Path("batches/public-merged") / DATE_TAG
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# Central dedup store (SQLite)
DEDUP = DedupStore()

def list_date_folder(date_tag: str) -> List[str]:
    """Single HF API call: list files in date folder (non-recursive)."""
    try:
        tree = API.list_repo_tree(
            repo_id=HF_REPO,
            path=date_tag,
            recursive=False,
            token=HF_TOKEN,
        )
    except Exception as e:
        # Fallback: try root listing if date folder absent
        tree = API.list_repo_tree(
            repo_id=HF_REPO,
            path="",
            recursive=False,
            token=HF_TOKEN,
        )
        # Filter by date prefix
        items = [t for t in tree if t.startswith(f"{date_tag}/")]
        return items
    return [t.path for t in tree]

def build_manifest(date_tag: str) -> List[str]:
    """Return sorted file paths for this date (CDN paths)."""
    files = list_date_folder(date_tag)
    # Deterministic ordering
    files = sorted(set(files))
    # Keep only files this shard is responsible for
    shard_files = [f for i, f in enumerate(files) if i % TOTAL_SHARDS == SHARD_ID]
    manifest_path = Path("manifest.json")
    manifest_path.write_text(json.dumps({"date": date_tag, "files": shard_files}, indent=2))
    return shard_files

def parse_to_pair(file_path: str, raw_bytes: bytes) -> Dict[str, str] | None:
    """
    Project heterogeneous file to {prompt, response}.
    Supports: .jsonl, .json, .parquet (via pyarrow), .txt heuristics.
    """
    import io
    suffix = Path(file_path).suffix.lower()

    try:
        if suffix == ".parquet":
            import pyarrow.parquet as pq
            table = pq.read_table(io.BytesIO(raw_bytes))
            # Heuristic projection: look for prompt/response/text/completion columns
            cols = table.column_names
            prompt_col = next((c for c in cols if "prompt" in c.lower()), None)
            response_col = next((c for c in cols if "response" in c.lower() or "completion" in c.lower() or "output" in c.lower()), None)
            if prompt_col and response_col and table.num_rows > 0:
                return {
                    "prompt": str(table[0][prompt_col].as_py()),
                    "response": str(table[0][response_col].as_py()),
                }
            # Fallback: first two columns
            if len(cols) >= 2 and table.num_rows > 0:
                return {
                    "prompt": str(table[0][cols[0]].as_py()),
                    "response": str(table[0][cols[1]].as_py()),
                }
            return None

        if suffix == ".jsonl":
            for line in raw_bytes.decode("utf-8", errors="ignore").strip().splitlines():
                obj = json.loads(line)
                prompt = obj.get("prompt") or obj.get("input") or obj.get("text")
                response = obj.get("response") or obj.get("output") or obj.get("completion")
                if prompt and response:
                    return {"prompt": str(prompt), "response": str(response)}
            return None

        if suffix == ".json":
            obj = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
            if isinstance(obj, list) and len(obj) > 0:
                obj = obj[0]
            prompt = obj.get("prompt") or obj.get("input") or obj.get("text")
            response = obj.get("response") or obj.get("output") or obj.get("completion")
            if prompt and response:
                return {"prompt": str(prompt), "response": str(response)}
            return None

        # .txt: simple Q/A split heuristic
        text = raw_bytes.decode("utf-8", errors="ignore").strip()
        if "\nA:" in text or "\nResponse:" in text or "\nAnswer:" in text:
            parts = text.split("\n", 1)
            prompt = parts[0].replace("Q:", "").replace("Question:", "").strip()
            response = parts[1].split("\n", 1)[-1].strip() if len(parts) > 1 else ""
            if prompt and response:
                return {"prompt": prompt, "response": response}
        return None
    except Exception as e:
        # Silently skip malformed files
        return None

def download_cdn(path: str) -> bytes:
    """CDN-only download (no auth header)."""
    url = f"{CDN_ROOT}/{path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

def upload_results(lines: List[str]) -> None:
    """Push shard output to HF dataset repo."""
    if not lines:
        return
    content = "\n".join(lines) + "\n"
    commit_msg = f"
