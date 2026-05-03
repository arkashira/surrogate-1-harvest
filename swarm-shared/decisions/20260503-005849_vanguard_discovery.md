# vanguard / discovery

### Final synthesized solution (correct + actionable)

**Core diagnosis (merged, de-duplicated)**
- No persisted `(repo, dateFolder)` manifest → every training run re-enumerates via authenticated HF API, burning quota and risking 429.  
- Recursive enumeration + pagination amplifies rate-limit pressure and exposes mixed-schema files and wasted I/O.  
- Training uses authenticated API calls during data load instead of CDN-only fetches.  
- No schema projection at parse time → `pyarrow.CastError` from heterogeneous files.  
- Lightning Studio idle-stop kills training and quota is wasted on repeated studio creation.

**Single, prioritized change**
Create a manifest build step and update training to use CDN-only URLs with schema projection and studio reuse. Do this once per `(repo, dateFolder)` and never enumerate again during training.

---

### 1. Build manifest (single authenticated non-recursive call per dateFolder)

`/opt/axentx/vanguard/scripts/build_manifest.py`
```bash
#!/usr/bin/env bash
# Build a CDN-only manifest for one repo/date folder to avoid HF API rate limits during training.
# Usage: build_manifest.py <repo> <date_folder> [out_dir]
# Example: build_manifest.py axentx/datasets 2026-05-03 ./manifests

set -euo pipefail

REPO="${1:-axentx/datasets}"
DATEFOLDER="${2:-$(date +%Y-%m-%d)}"
OUTDIR="${3:-./manifests}"
OUTFILE="${OUTDIR}/${REPO//\//_}/${DATEFOLDER}.json"

mkdir -p "$(dirname "$OUTFILE")"

python3 - "$REPO" "$DATEFOLDER" "$OUTFILE" <<'PY'
import os, json, sys
from huggingface_hub import list_repo_tree

def main(repo: str, datefolder: str, outfile: str):
    # Non-recursive top-level listing for the date folder
    tree = list_repo_tree(repo, path=datefolder, recursive=False)
    entries = []
    for item in tree:
        if getattr(item, "type", None) != "file":
            continue
        # CDN URL bypasses API auth/rate limits during training
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}"
        entries.append({
            "path": item.path,
            "size": getattr(item, "size", None),
            "cdn_url": cdn_url,
        })
    os.makedirs(os.path.dirname(outfile), exist_ok=True)
    with open(outfile, "w", encoding="utf-8") as f:
        json.dump({"repo": repo, "datefolder": datefolder, "files": entries}, f, indent=2)
    print(f"Wrote {len(entries)} entries to {outfile}")

if __name__ == "__main__":
    repo, datefolder, outfile = sys.argv[1], sys.argv[2], sys.argv[3]
    main(repo, datefolder, outfile)
PY

echo "Manifest built: $OUTFILE"
```

Run once (or in CI when new date folders appear):
```bash
python3 /opt/axentx/vanguard/scripts/build_manifest.py axentx/datasets 2026-05-03 ./manifests
```

---

### 2. Training script (CDN-only + schema projection + studio reuse)

`/opt/axentx/vanguard/train.py`
```python
#!/usr/bin/env python3
# Lightweight trainer that uses CDN-only manifest and Lightning Studio reuse.

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

try:
    from lightning import Lightning, Studio
except ImportError:
    print("Install lightning-sdk for orchestration: pip install lightning-sdk")
    Lightning = None

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "./manifests/axentx_datasets/2026-05-03.json")

# ---- Manifest & data utilities ----
def load_manifest(path: str) -> List[Dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("files", [])

def project_record(obj: Dict) -> Optional[Dict]:
    """
    Project heterogeneous record to {prompt, response}.
    Returns None for rows that cannot be projected.
    """
    if not isinstance(obj, dict):
        return None

    # Common field names (case-insensitive fallback)
    prompt_keys = {"prompt", "instruction", "input", "question", "query"}
    response_keys = {"response", "completion", "output", "answer"}

    p = None
    for k in prompt_keys:
        if k in obj and obj[k] is not None:
            p = str(obj[k]).strip()
            break
    r = None
    for k in response_keys:
        if k in obj and obj[k] is not None:
            r = str(obj[k]).strip()
            break

    if p is None or r is None:
        return None
    return {"prompt": p, "response": r}

def stream_records_from_manifest(manifest_path: str):
    """
    Yield projected {prompt, response} from files listed in manifest.
    Uses CDN URLs when possible; falls back to local paths.
    """
    files = load_manifest(manifest_path)
    if not files:
        raise ValueError(f"No files in manifest: {manifest_path}")

    # Prefer CDN; if unavailable, fall back to local path (e.g., in studio)
    for entry in files:
        source = entry.get("cdn_url") or entry.get("path")
        if not source:
            continue

        # Minimal per-format handlers; extend as needed
        try:
            if source.endswith(".jsonl"):
                import json
                import requests
                use_cdn = source.startswith("http")
                if use_cdn:
                    resp = requests.get(source, timeout=30)
                    resp.raise_for_status()
                    lines = resp.text.splitlines()
                else:
                    with open(source, encoding="utf-8") as f:
                        lines = f
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    rec = project_record(obj)
                    if rec:
                        yield rec

            elif source.endswith((".parquet", ".pq")):
                import pyarrow.parquet as pq
                table = pq.read_table(source if not source.startswith("http") else source)
                for batch in table.to_batches():
                    for col in batch.schema.names:
                        # normalize to utf-8 strings if needed
                        pass
                    # Convert batch to list of dicts (small batches) or use dataset library
                    # For simplicity and correctness, delegate to datasets if available:
                    try:
                        from datasets import load_dataset
                        ds = load_dataset("parquet", data_files={"train": [source]}, split="train", streaming=True)
                        for obj in ds:
                            rec = project_record(obj)
                            if rec:
                                yield rec
                        break
                    except Exception:
                        # fallback: convert batch to dicts
                        df = batch.to_pandas()
                        for _, row in df.iterrows():
                            rec = project_record(row.to_dict())
                            if rec:
                                yield rec
                        break

            elif source.endswith(".csv"):
                import csv
                import requests
                use_cdn = source.startswith("http")
                if use_cdn:
                    resp = requests.get(source, timeout=30)
                    resp.raise_for_status()
                    lines = resp.text.splitlines()
                    reader = csv.DictReader(lines)
                else:
                    with open(source, encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                for row in reader:
                    rec = project_record(row)
                    if rec:
                        yield rec

            else:
                # Unknown format: skip
                continue
        except Exception as e:
            # Log and skip problematic files to avoid blocking training
            print(f"Skipping {source} due to error: {e}", file=sys.stderr)
            continue

# ---- Lightning Studio utilities ----
def get_or_create_studio(
    name: str,
    teamspace: str = "default",
    machine: str = "cpu-small",
    idle_timeout_minutes: int = 30,
    max_retries: int = 3,
) -> Optional[Studio]:
    """
    Reuse an existing running studio or create one.
    Handles idle-stop by
