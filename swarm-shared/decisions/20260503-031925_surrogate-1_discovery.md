# surrogate-1 / discovery

### Final Consolidated Implementation (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env  
- Calls `list_repo_tree(path, recursive=False)` **once per shard per run** and saves a JSON manifest  
- Uses **deterministic shard assignment**: `hash(filename) % SHARD_TOTAL == SHARD_ID`  
- Downloads **only assigned files** via HF CDN (`resolve/main/...`) — no auth, no API rate limit  
- Projects heterogeneous schemas to `{prompt, response}` at parse time (eliminates pyarrow `CastError`)  
- Deduplicates via central md5 store (`lib/dedup.py`) with in-memory fallback  
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`  
- Returns exit code 0 on success, non-zero on fatal error (GitHub Actions handles retries)

---

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Environment:
  SHARD_ID        (int) 0..15
  SHARD_TOTAL     (int) default 16
  DATE            (str) YYYY-MM-DD (defaults to today UTC)
  HF_TOKEN        (str) write token for axentx/surrogate-1-training-pairs
  REPO_ID         (str) default axentx/surrogate-1-training-pairs
  LOG_LEVEL       (str) default INFO
"""
import os
import sys
import json
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

import requests
from huggingface_hub import HfApi, list_repo_tree

# ---- config ----
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("dataset-enrich")

# ---- helpers ----
def deterministic_shard(slug: str, total: int) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % total

def cdn_download(url: str, dest: Path) -> Path:
    """Download via HF CDN (no auth). Raises on non-2xx."""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return dest

def project_to_pair(raw_obj: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous HF dataset objects to {prompt, response}.
    Handles common surrogate-1 schema variants.
    """
    low = {k.lower().replace("-", "_"): v for k, v in raw_obj.items() if isinstance(v, (str, int, float, bool))}

    prompt = None
    response = None

    if "prompt" in low and "response" in low:
        prompt, response = low["prompt"], low["response"]
    elif "instruction" in low and "output" in low:
        prompt, response = low["instruction"], low["output"]
    elif "input" in low and "output" in low:
        prompt, response = low["input"], low["output"]
    elif "question" in low and "answer" in low:
        prompt, response = low["question"], low["answer"]
    elif "text" in low:
        text = str(low["text"])
        for sep in ("\nassistant:", "\nmodel:", "\nresponse:", "\n\n"):
            if sep in text:
                parts = text.split(sep)
                prompt = parts[0].strip()
                response = sep.strip().lstrip("\n") + ": " + sep.join(parts[1:]).strip()
                break
        if prompt is None:
            prompt, response = "", text
    else:
        prompt, response = "", json.dumps(raw_obj, ensure_ascii=False, default=str)

    return {"prompt": str(prompt), "response": str(response)}

# ---- dedup ----
def load_dedup_store() -> set:
    """Load central md5 store via lib/dedup.py if available; else in-memory."""
    try:
        from lib.dedup import DedupStore
        store = DedupStore()
        return set(store.list_hashes())
    except Exception as exc:
        log.warning("dedup store unavailable, using in-memory only: %s", exc)
        return set()

# ---- main ----
def main() -> int:
    if not HF_TOKEN:
        log.error("HF_TOKEN is required")
        return 1

    api = HfApi(token=HF_TOKEN)
    folder_path = f"batches/public-merged/{DATE}"

    # 1) List date folder once
    try:
        tree = list_repo_tree(repo_id=REPO_ID, path=folder_path, recursive=False)
    except Exception as exc:
        log.info("folder %s not found, starting empty: %s", folder_path, exc)
        tree = []

    json_files = [item.rfilename for item in tree if item.rfilename.endswith(".jsonl")]
    manifest_path = Path("manifest") / DATE / f"shard{SHARD_ID}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    assigned_files = [f for f in json_files if deterministic_shard(f, SHARD_TOTAL) == SHARD_ID]
    assigned_files.sort()

    manifest = {
        "repo_id": REPO_ID,
        "date": DATE,
        "shard_id": SHARD_ID,
        "shard_total": SHARD_TOTAL,
        "assigned_files": assigned_files,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    log.info("manifest saved: %s (%d assigned files)", manifest_path, len(assigned_files))

    # 2) Dedup store
    seen = load_dedup_store()
    new_hashes = set()

    # 3) Process assigned files
    out_dir = Path("batches") / "public-merged" / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{timestamp}.jsonl"

    total_in = 0
    total_out = 0
    total_bytes = 0

    for rfilename in assigned_files:
        cdn_url = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{rfilename}"
        log.info("downloading %s", rfilename)
        try:
            raw_bytes = cdn_download(cdn_url, Path("tmp") / rfilename.replace("/", "_"))
            total_bytes += len(raw_bytes)
        except Exception as exc:
            log.error("failed to download %s: %s", rfilename, exc)
            continue

        for line in raw_bytes.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            total_in += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.warning("invalid JSON in %s: %s", rfilename, line[:120])
                continue

            pair = project_to_pair(obj)
            payload = json.dumps(pair, ensure_ascii=False).encode("utf-8")
            h = hashlib.md5(payload).hexdigest()
            if h in seen or h in new_hashes:
                continue
            new_hashes.add(h)
            total_out += 1
            out_path.write_bytes(payload + b"\n")

    # 4) Append new hashes to central store if available
