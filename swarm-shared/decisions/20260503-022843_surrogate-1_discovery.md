# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Pre-lists files once via `list_repo_tree(path=DATE, recursive=False)` on the Mac orchestrator, saves `manifest.json` to repo, and embeds it in the worker so Lightning training can do **CDN-only fetches with zero API calls** during data load.
- Worker deterministically hashes each candidate file path (`hash(slug) % SHARD_TOTAL`) and only processes its shard.
- Downloads via raw CDN URL (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) — **bypasses `/api/` auth rate limits entirely**.
- Projects to `{prompt, response}` at parse time (avoids `pyarrow.CastError` on mixed schemas).
- Deduplicates via central `lib/dedup.py` md5 store.
- Writes output to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` (parquet later if desired) with no extra metadata columns.
- Reuses running HF Space when possible (quota savings) and respects Lightning idle-stop by checking status before `.run()`.

### Steps (≤2h)

1. Create `bin/dataset-enrich.py` (replaces shell script).
2. Add lightweight manifest creator `bin/create-manifest.py` (or embed optional fetch in runner).
3. Update `.github/workflows/ingest.yml` to pass `DATE` and optional manifest artifact.
4. Ensure `lib/dedup.py` is executable/importable and uses SQLite safely across processes.
5. Test locally with `HF_TOKEN` and a small date folder.

---

## bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Deterministic sharding + manifest-driven file list to avoid HF API during training.
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
from huggingface_hub import HfApi, hf_hub_download

# ── config --
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUTPUT_DIR = Path("batches/public-merged") / DATE
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# central dedup store
DEDUP_DB = Path(__file__).parent / "lib" / "dedup.py"
# We'll import the helper if available; fallback to local sqlite.
try:
    from lib.dedup import is_duplicate, mark_seen
except Exception:
    # minimal fallback: local sqlite in repo root
    import sqlite3
    DEDUP_PATH = Path("dedup_hashes.db")

    def _ensure_db():
        conn = sqlite3.connect(DEDUP_PATH)
        conn.execute("CREATE TABLE IF NOT EXISTS hashes (md5 TEXT PRIMARY KEY)")
        conn.commit()
        return conn

    _conn = _ensure_db()

    def is_duplicate(md5: str) -> bool:
        cur = _conn.execute("SELECT 1 FROM hashes WHERE md5=?", (md5,))
        return cur.fetchone() is not None

    def mark_seen(md5: str) -> None:
        try:
            _conn.execute("INSERT INTO hashes (md5) VALUES (?)", (md5,))
            _conn.commit()
        except sqlite3.IntegrityError:
            pass

# ── helpers --
def deterministic_shard(key: str, total: int) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % total

def load_manifest(manifest_path: Path) -> List[str]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    with open(manifest_path) as f:
        data = json.load(f)
    if isinstance(data, dict) and "files" in data:
        return data["files"]
    if isinstance(data, list):
        return data
    raise ValueError("Manifest must be list of paths or dict with 'files' key")

def list_repo_files_api(api: HfApi, repo_id: str, folder: str) -> List[str]:
    """
    Single API call to list files in a folder (non-recursive).
    Intended to be run by orchestrator; worker uses manifest.
    """
    items = api.list_repo_tree(repo_id=repo_id, path=folder, recursive=False)
    # items can be dict or objects depending on hf_hub version
    paths = []
    for it in items:
        if isinstance(it, dict):
            p = it.get("path")
        else:
            p = getattr(it, "path", None)
        if p:
            paths.append(p)
    return sorted(paths)

def download_via_cdn(repo_id: str, path_in_repo: str, dest: Path) -> Path:
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path_in_repo}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest

def normalize_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Project heterogeneous schemas to {prompt, response}.
    """
    prompt = rec.get("prompt") or rec.get("input") or rec.get("question") or ""
    response = rec.get("response") or rec.get("output") or rec.get("answer") or ""
    if not prompt and not response:
        return None
    return {"prompt": str(prompt), "response": str(response)}

# ── main --
def main() -> None:
    if not HF_TOKEN:
        print("ERROR: HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    api = HfApi(token=HF_TOKEN)
    manifest_path = Path("manifest.json")

    # If manifest exists, use it (preferred). Otherwise list once via API.
    if manifest_path.exists():
        all_files = load_manifest(manifest_path)
    else:
        print(f"Manifest not found at {manifest_path}. Listing via API (single call)...")
        all_files = list_repo_files_api(api, REPO_ID, DATE)
        # Save for reproducibility
        with open(manifest_path, "w") as f:
            json.dump({"date": DATE, "repo_id": REPO_ID, "files": all_files}, f)

    my_files = [p for p in all_files if deterministic_shard(p, SHARD_TOTAL) == SHARD_ID]
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} processing {len(my_files)} files (total {len(all_files)})")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = OUTPUT_DIR / f"shard{SHARD_ID}-{timestamp}.jsonl"

    processed = 0
    written = 0
    skipped_dup = 0
    failed = 0

    with out_path.open("w", encoding="utf-8") as out_f:
        for rel_path in my_files:
            processed += 1
            if processed % 10 == 0:
                print(f"  ... {processed}/{len(my_files)}")

            # compute content hash without downloading full dataset twice:
            # we'll download once, compute md5, dedup, then parse.
            try:
                local_file = Path("tmp") / rel_path.replace("/", "_")
                local_file.parent.mkdir(parents=True, exist_ok=True)
                download_via_cdn(REPO_ID, rel_path, local_file)
                content = local_file.read_bytes()
                md5 = hashlib.md5(content).hexdigest()

                if is_duplicate(md5):
                    skipped_dup += 1
                    local_file.unlink(missing_ok=True)

