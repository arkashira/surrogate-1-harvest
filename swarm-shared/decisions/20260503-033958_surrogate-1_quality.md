# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-first strategy**: single `list_repo_tree` call → saves `manifest.json` → workers use **CDN-only** downloads (`resolve/main/...`) to bypass HF API rate limits during data loading
- Projects heterogeneous files to `{prompt, response}` only at parse time (avoids pyarrow `CastError` on mixed schemas)
- Deduplicates via central md5 store (`lib/dedup.py`) and writes to deterministic shard outputs:  
  `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`
- Reuses running HF Space when possible (Lightning Studio reuse pattern) and respects commit-cap by deterministic shard→repo routing if scaled later

---

### Steps (1h 30m total)

1. **Create `bin/dataset-enrich.py`** (45m)  
   - Manifest fetch + CDN download loop  
   - Per-file schema-robust parser → `{prompt, response, md5, source_file}`  
   - Dedup via `lib/dedup.py`  
   - Shard-aware JSONL writer with timestamped filename

2. **Update `.github/workflows/ingest.yml`** (15m)  
   - Switch matrix job to run `python bin/dataset-enrich.py`  
   - Pass `SHARD_ID`, `SHARD_TOTAL`, `DATE_FOLDER` as env

3. **Add `requirements.txt` updates** (5m)  
   - Ensure `requests`, `tqdm`, `python-dotenv` available

4. **Remove/Deprecate `bin/dataset-enrich.sh`** (5m)  
   - Keep as symlink or backup for 1 week, then delete

5. **Smoke test** (20m)  
   - Local run with `SHARD_ID=0 SHARD_TOTAL=2 DATE_FOLDER=YYYY-MM-DD`  
   - Verify CDN-only fetches (no Authorization header in urllib logs) and correct shard outputs

---

### Code Snippets

#### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py [DATE_FOLDER]

Defaults:
  DATE_FOLDER = today (YYYY-MM-DD)
"""

import os
import sys
import json
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

import requests
from tqdm import tqdm

# ── config --
HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("ERROR: HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER") or datetime.date.today().isoformat()

# ── paths --
BASE_DIR = Path(__file__).parent.parent
LIB_DIR = BASE_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

try:
    from dedup import DedupStore
except ImportError:
    # fallback minimal dedup if lib not present
    class DedupStore:
        def __init__(self, db_path: str = ":memory:"):
            self.seen = set()
        def exists(self, md5: str) -> bool:
            return md5 in self.seen
        def add(self, md5: str) -> None:
            self.seen.add(md5)

OUT_DIR = BASE_DIR / "batches" / "public-merged" / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ── hf helpers --
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}
CDN_ROOT = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

API_ROOT = f"https://huggingface.co/api/{HF_REPO}"

def list_date_files(date_folder: str) -> List[str]:
    """
    Single API call to list files in date folder (non-recursive).
    Returns relative paths under date folder.
    """
    url = f"{API_ROOT}/tree/{date_folder}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 429:
        print("HF API 429 — wait 360s before retry", file=sys.stderr)
        sys.exit(1)
    resp.raise_for_status()
    items = resp.json()
    paths = []
    for item in items:
        if item.get("type") == "file":
            paths.append(f"{date_folder}/{item['path']}")
    return paths

def download_cdn(path: str, dest: Path) -> None:
    """Download via CDN (no auth header)."""
    url = f"{CDN_ROOT}/{path}"
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

# ── schema-robust parser --
def normalize_pair(raw: Dict[str, Any], source_file: str) -> Optional[Dict[str, Any]]:
    """
    Project heterogeneous file to {prompt, response} + metadata.
    Returns None if unusable.
    """
    if not isinstance(raw, dict):
        return None

    # Common field variants
    prompt = (
        raw.get("prompt")
        or raw.get("instruction")
        or raw.get("input")
        or raw.get("question")
    )
    response = (
        raw.get("response")
        or raw.get("output")
        or raw.get("answer")
        or raw.get("completion")
    )

    if not isinstance(prompt, str) or not isinstance(response, str):
        return None

    prompt = prompt.strip()
    response = response.strip()
    if not prompt or not response:
        return None

    content = f"{prompt}\n\n{response}"
    md5 = hashlib.md5(content.encode("utf-8")).hexdigest()

    return {
        "prompt": prompt,
        "response": response,
        "md5": md5,
        "source_file": source_file,
    }

# ── worker --
def run_shard() -> None:
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} | date={DATE_FOLDER}")

    # 1) manifest
    print("Fetching manifest...")
    files = list_date_files(DATE_FOLDER)
    if not files:
        print("No files in date folder — exiting.")
        return

    # deterministic shard assignment by filename hash
    def assign_shard(filepath: str) -> int:
        h = int(hashlib.md5(filepath.encode()).hexdigest(), 16)
        return h % SHARD_TOTAL

    my_files = [f for f in files if assign_shard(f) == SHARD_ID]
    print(f"Processing {len(my_files)} files for shard {SHARD_ID}")

    # 2) dedup store (central sqlite on HF Space would be ideal; local fallback)
    dedup = DedupStore(str(BASE_DIR / "dedup-central.sqlite"))

    written = 0
    skipped_dup = 0
    failed_parse = 0

    with open(OUT_FILE, "w", encoding="utf-8") as out_f:
        for rel_path in tqdm(my_files, desc="Ingesting"):
            tmp_file = OUT_DIR / "_tmp_download"
            try:
                download_cdn(rel_path, tmp_file)

                # Try JSONL first (common), then single JSON
                try:
                    with open(tmp_file, "r", encoding="utf-8") as f:
                        for line
