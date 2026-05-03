# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Uses **one** `list_repo_tree` call (per date folder) to build a deterministic `manifest.json`
- Downloads only assigned-shard files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with **no Authorization header during data transfer**
- Projects each file to `{prompt, response}` at parse time (avoids pyarrow `CastError` on mixed schemas)
- Deduplicates via central `lib/dedup.py` (content hash store)
- Outputs: `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Keeps GitHub Actions matrix (16 shards) unchanged; only the worker implementation changes

### Why this is the highest-value incremental improvement
- Eliminates HF API rate-limit risk during bulk ingestion (CDN bypass)
- Avoids `load_dataset(streaming=True)` mixed-schema failures
- Preserves existing parallelism and infra (16 shards, Actions matrix)
- Fits within 2h: small, focused worker; reuses existing dedup lib

---

## Code snippets

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Environment:
  SHARD_ID (int): 0..15
  SHARD_TOTAL (int): default 16
  DATE (str): YYYY-MM-DD folder on HF dataset repo
  HF_TOKEN (str): write token for axentx/surrogate-1-training-pairs
  REPO (str): default "axentx/surrogate-1-training-pairs"
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from huggingface_hub import HfApi

# ── config ──────────────────────────────────────────────────────────────
REPO = os.getenv("REPO", "axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
BASE_DIR = Path(__file__).parent.parent
DEDUP_PY = BASE_DIR / "lib" / "dedup.py"
OUTPUT_DIR = BASE_DIR / "batches" / "public-merged" / DATE
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TS = datetime.now(timezone.utc).strftime("%H%M%S")
OUTFILE = OUTPUT_DIR / f"shard{SHARD_ID}-{TS}.jsonl"
MANIFEST_PATH = BASE_DIR / "manifest.json"

# ── helpers ─────────────────────────────────────────────────────────────
def deterministic_shard(key: str, n: int) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % n

def list_date_files(date_folder: str) -> List[str]:
    """Single API call: list files in date folder (non-recursive)."""
    try:
        tree = API.list_repo_tree(repo_id=REPO, path=date_folder, recursive=False)
        files = [t.r_path for t in tree if not t.r_path.endswith("/")]
    except Exception:
        # Fallback: list repo root and filter by prefix
        tree = API.list_repo_tree(repo_id=REPO, path="", recursive=False)
        files = [t.r_path for t in tree if t.r_path.startswith(f"{date_folder}/")]
    return sorted(set(files))

def build_manifest(date_folder: str, out_path: Path) -> List[str]:
    files = list_date_files(date_folder)
    manifest = {
        "repo": REPO,
        "date_folder": date_folder,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    out_path.write_text(json.dumps(manifest, indent=2))
    return files

def cdn_download(repo: str, path: str, dest: Path) -> bool:
    """Download via CDN without Authorization header."""
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except Exception as exc:
        print(f"CDN download failed {path}: {exc}", file=sys.stderr)
        return False

def project_to_pair(raw_bytes: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Project heterogeneous file to {prompt, response} pairs.
    Best-effort parsers; extend per observed schema.
    """
    import io

    pairs = []
    name = filename.lower()

    # JSONL
    if name.endswith(".jsonl"):
        for line in io.TextIOWrapper(io.BytesIO(raw_bytes), encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
            response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
            if prompt and response:
                pairs.append({"prompt": str(prompt), "response": str(response)})
        return pairs

    # JSON (single object or list)
    if name.endswith(".json"):
        try:
            obj = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            return pairs
        items = obj if isinstance(obj, list) else [obj]
        for item in items:
            if not isinstance(item, dict):
                continue
            prompt = item.get("prompt") or item.get("input") or item.get("question") or ""
            response = item.get("response") or item.get("output") or item.get("answer") or ""
            if prompt and response:
                pairs.append({"prompt": str(prompt), "response": str(response)})
        return pairs

    # CSV
    if name.endswith(".csv"):
        import csv
        try:
            text = io.TextIOWrapper(io.BytesIO(raw_bytes), encoding="utf-8")
            reader = csv.DictReader(text)
            for row in reader:
                prompt = row.get("prompt") or row.get("input") or row.get("question") or ""
                response = row.get("response") or row.get("output") or row.get("answer") or ""
                if prompt and response:
                    pairs.append({"prompt": str(prompt), "response": str(response)})
        except Exception:
            pass
        return pairs

    # Fallback: plain text with simple double-newline separation
    try:
        text = raw_bytes.decode("utf-8")
    except Exception:
        return pairs

    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    if len(blocks) >= 2:
        pairs.append({"prompt": blocks[0], "response": blocks[1]})
    return pairs

def import_dedup() -> Optional[Any]:
    if not DEDUP_PY.exists():
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("dedup", DEDUP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ── main ───────────────────────────────────────────────────────────────
def main() -> None:
    date_folder = DATE
    manifest_path = MANIFEST_PATH

    # Build or reuse manifest
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        files = manifest["files"]
    else:
        files = build_manifest(date_folder, manifest_path)

    my_files = [f for f in files if deterministic_shard(f, SHARD_TOTAL
