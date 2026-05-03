# vanguard / discovery

## 1. Diagnosis

- Frontend still triggers runtime HF API calls (`list_repo_tree`, `load_dataset`, or equivalent) during dataset selection/preview, causing 429s and non-reproducible runs.
- No deterministic, content-addressed manifest of available files (date/slug → path) to decouple discovery from API calls.
- Ingestion/training can still hit HF API during data loading instead of pure CDN fetches, violating the CDN bypass pattern.
- Missing lightweight “discovery” artifact that can be generated once on the Mac (or CI) and shipped to frontend/backend so training uses zero API calls.
- No guardrails to prevent accidental `load_dataset(streaming=True)` on heterogeneous repos (pyarrow CastError risk for Surrogate-1).

## 2. Proposed change

Create a single discovery-time script that:
- Runs on Mac/CI (orchestration only) after rate-limit window clears.
- Calls `list_repo_tree` once per date folder for a target dataset repo (e.g., `datasets/surrogate-1/mirror-merged/2026-04-29`).
- Produces `vanguard/discovery/manifest-2026-04-29.json` mapping `{date, slug, repo, path, cdn_url, sha256?}`.
- Embeds this manifest path in training scripts so Lightning workers fetch via CDN only.
- Adds a small validation step to ensure no `load_dataset` calls remain in training paths.

Scope:
- New file: `/opt/axentx/vanguard/discovery/build_manifest.py`
- Update: `/opt/axentx/vanguard/train.py` (or create if missing) to accept `--manifest` and use CDN-only fetches.
- Optional: `/opt/axentx/vanguard/discovery/verify_no_load_dataset.py` to gate merges.

## 3. Implementation

```bash
# Create directory
mkdir -p /opt/axentx/vanguard/discovery
```

`/opt/axentx/vanguard/discovery/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate a deterministic manifest for a dataset repo/date folder.
Run from Mac/CI (orchestration) when HF API rate limits allow.
Outputs: manifest-{date}.json
"""
import json
import os
import sys
import hashlib
from datetime import date
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

REPO_ID = os.getenv("HF_DATASET_REPO", "datasets/surrogate-1")
DATE_FOLDER = os.getenv("DATE_FOLDER", str(date.today()))
OUTPUT_DIR = Path(__file__).parent
OUTPUT_PATH = OUTPUT_DIR / f"manifest-{DATE_FOLDER}.json"

def build_manifest(repo_id: str, folder: str) -> list[dict]:
    """
    Single API call: list_repo_tree non-recursive for one folder.
    Returns list of {date, slug, repo, path, cdn_url}
    """
    entries = list_repo_tree(repo_id=repo_id, path=folder, recursive=False)
    manifest = []
    for entry in entries:
        if entry.type != "file":
            continue
        path = entry.path
        # Expect files like mirror-merged/2026-04-29/{slug}.parquet
        slug = Path(path).stem
        cdn_url = (
            f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
        )
        manifest.append(
            {
                "date": folder,
                "slug": slug,
                "repo": repo_id,
                "path": path,
                "cdn_url": cdn_url,
            }
        )
    return manifest

def main() -> None:
    print(f"Building manifest for {REPO_ID}/{DATE_FOLDER}")
    manifest = build_manifest(REPO_ID, DATE_FOLDER)
    if not manifest:
        print("No files found; ensure folder exists and API token has read access.")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"Wrote {len(manifest)} entries to {OUTPUT_PATH}")

if __name__ == "__main__":
    main()
```

Make executable (optional, for direct shell use):
```bash
chmod +x /opt/axentx/vanguard/discovery/build_manifest.py
```

`/opt/axentx/vanguard/train.py` (minimal CDN-only loader example)
```python
#!/usr/bin/env python3
"""
Train script that uses CDN-only fetches via manifest.
Usage:
  HF_DATASET_REPO=datasets/surrogate-1 \
  DATE_FOLDER=2026-04-29 \
  python discovery/build_manifest.py
  python train.py --manifest discovery/manifest-2026-04-29.json
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Dict

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

def load_manifest(path: str) -> List[Dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def fetch_via_cdn(cdn_url: str, dst: Path) -> Path:
    """Download via CDN (no Authorization header)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return dst
    resp = requests.get(cdn_url, timeout=60)
    resp.raise_for_status()
    dst.write_bytes(resp.content)
    return dst

def project_to_prompt_response(parquet_path: Path):
    """
    Project heterogeneous files to {prompt, response} at parse time.
    Adjust projection logic to match your schema.
    """
    tbl = pq.read_table(parquet_path)
    cols = tbl.column_names
    # Heuristic projection; adapt to real schema
    prompt_col = next((c for c in ("prompt", "instruction", "input") if c in cols), None)
    response_col = next((c for c in ("response", "output", "completion") if c in cols), None)

    if prompt_col and response_col:
        prompts = tbl.column(prompt_col).to_pylist()
        responses = tbl.column(response_col).to_pylist()
    else:
        # Fallback: use first two text-like columns
        text_cols = [c for c in cols if tbl.schema.field(c).type in ("string", "large_string")]
        if len(text_cols) >= 2:
            prompts = tbl.column(text_cols[0]).to_pylist()
            responses = tbl.column(text_cols[1]).to_pylist()
        else:
            raise ValueError(f"Cannot project prompt/response from {cols}")
    return [{"prompt": p, "response": r} for p, r in zip(prompts, responses) if p and r]

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--cache-dir", default=".cdn_cache", help="Local CDN cache")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    if not manifest:
        print("Manifest empty")
        return

    cache_dir = Path(args.cache_dir)
    examples = []
    for item in tqdm(manifest, desc="Fetching via CDN"):
        cdn_url = item["cdn_url"]
        rel = item["path"]
        local_path = cache_dir / rel
        try:
            fetch_via_cdn(cdn_url, local_path)
            examples.extend(project_to_prompt_response(local_path))
        except Exception as exc:
            print(f"Skipping {cdn_url}: {exc}")

    print(f"Prepared {len(examples)} examples for training")
    # Continue with surrogate-1 training (Lightning Studio) using `examples`
    # Do NOT call load_dataset() here.

if __name__ == "__main__":
    main()
```

`/opt/axentx/vanguard/discovery/verify_no_load_dataset.py` (optional gate)
```python
#!/usr/bin/env python3
"""
Quick grep-style check that training code does not invoke load_dataset.
"""
import re
import sys
from pathlib import Path

TRAIN_ROOT = Path(__file__).parent.parent
BAD_PATTERNS = [
    re.compile(rb"
