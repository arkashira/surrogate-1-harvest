# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env  
- Uses **one** `list_repo_tree` call (per DATE folder) → saves `manifest.json`  
- Each shard deterministically hashes slugs → picks its slice  
- Downloads via **raw CDN URLs** (`resolve/main/...`) with **zero HF API calls during stream**  
- Projects to `{prompt, response}` only at parse time (avoids pyarrow CastError)  
- Dedups via central `lib/dedup.py` md5 store  
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`  
- Returns exit 0 only if ≥1 new pair uploaded; otherwise exits 0 with log “no new pairs”

---

## bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage (env):
  SHARD_ID=0..15
  SHARD_TOTAL=16
  DATE=2026-05-03
  HF_TOKEN=hf_xxx
  REPO=datasets/axentx/surrogate-1-training-pairs
  DRY_RUN=1  (optional)
"""

import os
import sys
import json
import hashlib
import datetime
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from huggingface_hub import HfApi

# ── config --
REPO = os.getenv("REPO", "datasets/axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.date.today().isoformat())
DRY_RUN = bool(os.getenv("DRY_RUN"))
API = HfApi(token=HF_TOKEN)

# ── paths --
WORKDIR = Path(__file__).parent.parent
MANIFEST_DIR = WORKDIR / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)
DEDUP_DIR = WORKDIR / "lib"
DEDUP_DIR.mkdir(exist_ok=True, parents=True)
DEDUP_DB = DEDUP_DIR / "dedup.py"

# ── helpers --
def deterministic_shard(slug: str) -> int:
    """Map slug -> [0, SHARD_TOTAL). Stable across runs."""
    digest = hashlib.sha256(slug.encode()).digest()
    return int.from_bytes(digest, "big") % SHARD_TOTAL

def list_date_folder() -> List[str]:
    """Single API call: list files under repo root or date subfolder."""
    try:
        tree = API.list_repo_tree(repo_id=REPO, path=DATE, recursive=False)
        prefix = f"{DATE}/"
    except Exception:
        tree = API.list_repo_tree(repo_id=REPO, path="", recursive=False)
        prefix = ""
    paths = [
        prefix + f.rfilename
        for f in tree
        if f.rfilename.endswith((".parquet", ".jsonl", ".json"))
    ]
    return sorted(paths)

def save_manifest(paths: List[str]) -> Path:
    manifest_path = MANIFEST_DIR / f"manifest-{DATE}.json"
    manifest_path.write_text(json.dumps({"date": DATE, "paths": paths}, separators=(",", ":")))
    return manifest_path

def load_manifest() -> Optional[List[str]]:
    manifest_path = MANIFEST_DIR / f"manifest-{DATE}.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())["paths"]
    return None

def cdn_url(path: str) -> str:
    """CDN bypass URL (no auth, no API rate-limit)."""
    return f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"

# ── schema projection --
def try_project_to_pair(obj) -> Optional[Dict[str, str]]:
    """
    Best-effort projection to {prompt, response}.
    Supports common surrogate-1 schemas.
    """
    if isinstance(obj, dict):
        if "prompt" in obj and "response" in obj:
            return {"prompt": str(obj["prompt"]), "response": str(obj["response"])}
        prompt_keys = {"prompt", "instruction", "input", "question", "user"}
        response_keys = {"response", "output", "answer", "assistant", "completion"}
        pk = next((k for k in obj if k in prompt_keys), None)
        rk = next((k for k in obj if k in response_keys), None)
        if pk and rk:
            return {"prompt": str(obj[pk]), "response": str(obj[rk])}
        if "text" in obj:
            t = str(obj["text"])
            if "\n\n" in t:
                p, r = t.split("\n\n", 1)
                return {"prompt": p.strip(), "response": r.strip()}
    return None

# ── dedup bridge --
def ensure_dedup_module() -> None:
    """Create lib/dedup.py if missing (simple md5 set)."""
    if DEDUP_DB.exists():
        return
    DEDUP_DB.write_text("""\
import json
from pathlib import Path

_DB = Path(__file__).parent / "dedup.jsonl"

def is_duplicate(md5_b: bytes) -> bool:
    h = md5_b.hex()
    if not _DB.exists():
        return False
    with _DB.open() as f:
        for line in f:
            if line.strip() == h:
                return True
    return False

def add_md5(md5_b: bytes) -> None:
    h = md5_b.hex()
    with _DB.open("a") as f:
        f.write(h + "\\\\n")
""")

def is_duplicate(md5_b: bytes) -> bool:
    ensure_dedup_module()
    try:
        sys.path.insert(0, str(DEDUP_DIR))
        from dedup import is_duplicate as _is_duplicate
        return _is_duplicate(md5_b)
    except Exception:
        if not hasattr(is_duplicate, "_seen"):
            is_duplicate._seen = set()  # type: ignore[attr-defined]
        h = md5_b.hex()
        if h in is_duplicate._seen:  # type: ignore[attr-defined]
            return True
        is_duplicate._seen.add(h)  # type: ignore[attr-defined]
        return False

def add_md5(md5_b: bytes) -> None:
    ensure_dedup_module()
    try:
        sys.path.insert(0, str(DEDUP_DIR))
        from dedup import add_md5 as _add_md5
        _add_md5(md5_b)
    except Exception:
        pass

# ── stream & process --
def process_file(path: str) -> List[Dict[str, str]]:
    """Download via CDN, parse, project, dedup -> list of new pairs."""
    url = cdn_url(path)
    suffix = Path(path).suffix.lower()
    pairs: List[Dict[str, str]] = []

    if suffix == ".parquet":
        import pyarrow.parquet as pq

        with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
            r = requests.get(url, stream=True, timeout=120)
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=8192 * 32):
                tmp.write(chunk)
            tmp.flush()

            try:
                table = pq.read_table(tmp.name, columns=["prompt", "response"])
            except Exception:
                table = pq.read_table(tmp.name)

        for batch in table.to_batches(max_chunksize=8192):
            cols = batch.column_names
            if "prompt" in cols and "response" in cols:
                prompts = batch.column("prompt").to_pylist()
                responses = batch.column("response").to_pylist()
                for p, r in zip(prompts, responses):
                    if p is None or r is None:

