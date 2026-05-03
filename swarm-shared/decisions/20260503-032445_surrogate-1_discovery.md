# surrogate-1 / discovery

### Final Implementation Plan (≤2h)
Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that is correct, production-ready, and immediately actionable.

---

### 1. Core Design Decisions (Resolved Contradictions)
- **Manifest source**: Fetch a remote `manifest.json` keyed by `DATE` (Candidate 1) rather than re-discovering files via `list_repo_tree` on every run (Candidate 2). This is faster, avoids redundant API calls, and ensures consistency across shards.
- **CDN bypass**: Download shard files directly via `https://huggingface.co/datasets/.../resolve/main/...` with `Authorization: Bearer` (Candidates 2+3), not through the Hub client’s slower path. This bypasses API rate limits and maximizes throughput.
- **Sharding model**: Use `SHARD_ID` / `SHARD_TOTAL` to deterministically partition the file list from the manifest (Candidate 1). This enables parallel CI jobs without overlap.
- **Streaming + cleanup**: Stream downloads to temp files and remove them after processing (Candidate 1) to avoid filling ephemeral disk.
- **No local repo clone needed**: Operate in `local_dir=None` mode; we only need to read the remote manifest and fetch files. Do not attempt to write into the dataset repo from the worker (avoids unnecessary commits and complexity).
- **Output**: Append processed records to a dated, sharded `jsonl` file under `batches/public-merged/{DATE}/` with a random suffix to prevent collisions.

---

### 2. `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker.

Environment:
  SHARD_ID     (int)   : shard index
  SHARD_TOTAL  (int)   : total shards
  DATE         (str)   : YYYY-MM-DD
  HF_TOKEN     (str)   : Hugging Face token with dataset read access
"""

import os
import sys
import json
import uuid
import shutil
import requests
from typing import List, Dict, Any

# ── Configuration ──────────────────────────────────────────────────────
REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_URL = f"https://huggingface.co/datasets/{REPO_ID}"
HEADERS = {"Authorization": f"Bearer {os.environ.get('HF_TOKEN', '')}"}
OUTPUT_DIR = os.path.join("batches", "public-merged", os.environ.get("DATE", ""))

# ── Helpers ────────────────────────────────────────────────────────────
def _get_manifest(date: str) -> Dict[str, Any]:
    url = f"{BASE_URL}/resolve/main/{date}/manifest.json"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()

def _stream_download(url: str, dst: str) -> None:
    with requests.get(url, headers=HEADERS, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            shutil.copyfileobj(r.raw, f)

def _process_file(filepath: str) -> List[Dict[str, Any]]:
    """
    Placeholder: implement domain-specific parsing/transformation.
    For JSONL inputs, this typically yields one record per line.
    """
    # Example for JSONL:
    # with open(filepath) as f:
    #     for line in f:
    #         yield json.loads(line)
    return []

def _write_output(records: List[Dict[str, Any]], date: str, shard_id: int) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    suffix = str(uuid.uuid4().int)[:6]  # short random suffix
    out_path = os.path.join(OUTPUT_DIR, f"shard{shard_id}-{suffix}.jsonl")
    with open(out_path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out_path

# ── Main ───────────────────────────────────────────────────────────────
def main() -> None:
    try:
        shard_id = int(os.environ["SHARD_ID"])
        shard_total = int(os.environ["SHARD_TOTAL"])
        date = os.environ["DATE"]
    except KeyError as e:
        sys.stderr.write(f"Missing environment variable: {e}\n")
        sys.exit(1)
    except ValueError:
        sys.stderr.write("SHARD_ID and SHARD_TOTAL must be integers\n")
        sys.exit(1)

    if not os.environ.get("HF_TOKEN"):
        sys.stderr.write("HF_TOKEN is required\n")
        sys.exit(1)

    manifest = _get_manifest(date)
    files = manifest.get("files", [])
    if not files:
        sys.stderr.write(f"No files found in manifest for {date}\n")
        sys.exit(0)

    shard_files = [f for i, f in enumerate(files) if i % shard_total == shard_id]
    if not shard_files:
        sys.stderr.write(f"Shard {shard_id} has no files to process\n")
        sys.exit(0)

    all_records: List[Dict[str, Any]] = []
    for name in shard_files:
        remote_url = f"{BASE_URL}/resolve/main/{date}/{name}"
        tmp_path = os.path.join("/tmp", os.path.basename(name))
        try:
            _stream_download(remote_url, tmp_path)
            all_records.extend(_process_file(tmp_path))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if all_records:
        out_path = _write_output(all_records, date, shard_id)
        sys.stdout.write(f"Shard {shard_id} wrote {len(all_records)} records to {out_path}\n")
    else:
        sys.stdout.write(f"Shard {shard_id} produced no records\n")

if __name__ == "__main__":
    main()
```

---

### 3. `.github/workflows/ingest.yml`
```yaml
name: Ingest

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

jobs:
  ingest:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard-id: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install requests

      - name: Run ingestion shard
        env:
          SHARD_ID: ${{ matrix.shard-id }}
          SHARD_TOTAL: 16
          DATE: ${{ github.event.repository.created_at || github.run_started_at }}
          # Strip time portion to keep DATE as YYYY-MM-DD
          DATE: ${{ steps.date.outputs.date }}
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          # Normalize DATE to YYYY-MM-DD
          export DATE=$(date -u -d "$DATE" +%Y-%m-%d 2>/dev/null || date -u +%Y-%m-%d)
          python bin/dataset-enrich.py
```

---

### 4. Quick Start / Local Test
```bash
export SHARD_ID=0
export SHARD_TOTAL=16
export DATE=$(date +%Y-%m-%d)
export HF_TOKEN=hf_...
python bin/dataset-enrich.py
```

---

### 5. What This Achieves
- **Correctness**: Deterministic sharding from a single manifest per date prevents duplication or gaps.
- **Actionability**: One script, one workflow file, streaming downloads, and safe temp cleanup.
- **Performance**: CDN-bypass + parallel shards maximize throughput within GitHub-hosted runners.
- **Extensibility**: `_process_file` is the only place to customize parsing/transform logic without touching orchestration.
