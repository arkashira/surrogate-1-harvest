# airship / discovery

Candidate 3:
## Highest-Value Incremental Improvement (<2h)

**Goal**: Harden `airship discover` into a deterministic, **CDN-only** orchestrator that eliminates HF API rate limits and PyArrow schema errors while producing **reproducible file lists**.

**Why this ships fast**:
- No new infra — uses existing docker-compose + HF CDN + local JSON manifest.
- Single focused script (`discover.py`) + one config change.
- Removes two critical failure modes (429 rate limits, mixed-schema CastError) that block surrogate training.

---

## Implementation Plan (≤2h)

| Step | Owner | Time | Deliverable |
|------|-------|------|-------------|
| 1. Audit current `discover` entrypoint | me | 10m | Confirm script location & args |
| 2. Implement CDN-only file lister | me | 45m | `discover.py` with `list_repo_tree` → JSON manifest |
| 3. Add schema-safe parser stub | me | 20m | `parse_for_prompt_response()` that never loads full dataset |
| 4. Wire manifest into surrogate training contract | me | 20m | `file_list.json` consumed by `train.py` (CDN-only reads) |
| 5. Update docker-compose + README snippet | me | 15m | One-liner to run discovery, volume mount for manifest |
| 6. Smoke test (local + container) | me | 10m | Manifest generated, no HF API calls during parse |

---

## Code Snippets

### 1. `airship/discover.py` (new)

```python
#!/usr/bin/env python3
"""
CDN-only discovery for HF datasets.
Produces reproducible file_list.json for surrogate training.
Avoids:
- HF API rate limits (uses CDN URLs)
- PyArrow CastError (never loads full dataset)
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict

import requests
from huggingface_hub import HfApi, hf_hub_download

# --
# Config (override via env)
# --
REPO_ID = os.getenv("HF_DATASET_REPO", "your-org/your-dataset")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
OUTPUT_DIR = Path(os.getenv("DISCOVER_OUT", "/opt/axentx/airship/discover_out"))
MANIFEST_PATH = OUTPUT_DIR / "file_list.json"
CDN_BASE = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

api = HfApi()

def list_date_folder_files(repo_id: str, date_folder: str) -> List[Dict]:
    """
    Single API call: list_repo_tree non-recursive for one date folder.
    Returns minimal metadata for CDN fetch.
    """
    try:
        tree = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
    except Exception as exc:
        # If rate-limited, wait and retry once (backstop)
        print(f"[discover] list_repo_tree failed: {exc}. Waiting 360s...", file=sys.stderr)
        time.sleep(360)
        tree = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)

    files = []
    for entry in tree:
        if entry.type == "file":
            files.append(
                {
                    "repo_id": repo_id,
                    "path": f"{date_folder}/{entry.path}",
                    "cdn_url": f"{CDN_BASE}/{date_folder}/{entry.path}",
                    "size": getattr(entry, "size", None),
                }
            )
    return files

def write_manifest(files: List[Dict], manifest_path: Path) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "repo_id": REPO_ID,
        "date_folder": DATE_FOLDER,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }
    manifest_path.write_text(json.dumps(payload, indent=2))
    print(f"[discover] Manifest written: {manifest_path} ({len(files)} files)")

def parse_for_prompt_response(file_path: Path) -> Dict:
    """
    Schema-safe projection: extract only {prompt, response} at parse time.
    Avoids load_dataset(streaming=True) on heterogeneous repos.
    """
    # Lightweight: read line-by-line or use pyarrow only on projected cols
    # Placeholder: implement per-repo parser registry if needed.
    # For now, return minimal contract.
    return {"prompt": "", "response": "", "source_file": str(file_path)}

def main() -> None:
    print(f"[discover] Listing {REPO_ID}/{DATE_FOLDER} (CDN-only strategy)")
    files = list_date_folder_files(REPO_ID, DATE_FOLDER)
    if not files:
        print("[discover] No files found. Exiting.", file=sys.stderr)
        sys.exit(1)

    write_manifest(files, MANIFEST_PATH)

    # Optional: download one sample to validate projection logic
    sample = files[0]
    local_sample = OUTPUT_DIR / Path(sample["path"]).name
    if not local_sample.exists():
        hf_hub_download(repo_id=REPO_ID, filename=sample["path"], local_dir=OUTPUT_DIR)
    _ = parse_for_prompt_response(local_sample)

    print("[discover] Done. Use manifest in surrogate training (CDN-only reads).")

if __name__ == "__main__":
    main()
```

### 2. `surrogate/train.py` (CDN-only data loader snippet)

```python
import json
from pathlib import Path
import requests

MANIFEST = Path("/opt/axentx/airship/discover_out/file_list.json")

def load_file_manifest() -> dict:
    return json.loads(MANIFEST.read_text())

def cdn_lines(cdn_url: str):
    # CDN fetch: no Authorization header → bypasses /api/ rate limits
    resp = requests.get(cdn_url, timeout=30)
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if line:
            yield line

def build_cdn_dataset(manifest_path=MANIFEST):
    manifest = json.loads(manifest_path.read_text())
    for f in manifest["files"]:
        yield from cdn_lines(f["cdn_url"])
```

### 3. Docker / compose snippet (add to `docker-compose.microservices.yml` or run standalone)

```yaml
  discover:
    image: python:3.11-slim
    volumes:
      - ./airship/discover_out:/opt/axentx/airship/discover_out
      - ./airship:/opt/axentx/airship
    environment:
      - HF_DATASET_REPO=your-org/your-dataset
      - HF_DATE_FOLDER=2026-05-02
    command: ["python", "/opt/axentx/airship/discover.py"]
```

### 4. One-liner to run discovery

```bash
HF_DATASET_REPO=your-org/your-dataset \
HF_DATE_FOLDER=2026-05-02 \
python airship/discover.py
```

---

## Verification (smoke test)

```bash
# 1) Generate manifest
HF_DATASET_REPO=your-org/your-dataset python airship/discover.py

# 2) Confirm CDN-only URLs in manifest
jq '.files[0].cdn_url' airship/discover_out/file_list.json

# 3) Confirm no HF API calls during parse (should not trigger 429)
#    (Monitor with `grep -i "429" ~/.cache/huggingface/...` or network log)
```

---

## Notes & Trade-offs

- Uses **single `list_repo_tree` call per date folder** (avoids recursive pagination and 429).
- **CDN downloads** are not counted against HF API rate limits — key insight applied.
- **No `load_dataset`** during discovery — avoids PyArrow CastError on mixed schemas.
- Manifest is **reproducible** (same date folder → same list) and can be committed for audit.
- Training script consumes manifest and does **pure CDN fetches** during data load (zero API calls).
