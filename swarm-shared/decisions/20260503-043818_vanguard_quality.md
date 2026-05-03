# vanguard / quality

## Final Synthesis (chosen from strongest parts, corrected + actionable)

**Core diagnosis (merged, corrected)**
- No content-addressed manifest per date folder causes runtime repo enumeration via `list_repo_tree`/`load_dataset` and triggers HF API 429s, non-reproducible epochs, and quota waste.
- Missing deterministic `{path, sha256, size}` snapshot means CDN-only fetches cannot be validated or resumed; partial/corrupt downloads silently poison training and risk pyarrow `CastError`/schema mismatch.
- Scripts enumerate the repo on every run instead of using a pinned manifest + CDN-only fetches.
- No schema-projection guard before upload allows malformed files into `dataset-mirror` (schema drift, mixed dtypes).
- No lightweight integrity check before training starts; corrupted files are discovered mid-epoch, wasting compute.

**Proposed change (merged, prioritized)**
1. Add a single source-of-truth manifest generator that:
   - Accepts repo and date folder.
   - Calls `list_repo_tree(path, recursive=True)` once (recursive to catch nested date layouts) and produces `manifest-{date}.json` with `{path, sha256, size, meta?}`.
   - Stores manifests under version control or alongside training configs so training jobs reference them by content hash.
2. Patch/create the training entrypoint to:
   - Accept a manifest path and repo.
   - Use CDN-only fetches with zero HF API calls during training.
   - Validate `sha256` on download (fail-fast) and stream to avoid OOM.
   - Project schema early (e.g., enforce `{prompt, response}` fields and dtypes) before yielding items to the trainer.
3. Add pre-ingest validation:
   - Lightweight schema check and file-type allowlist before upload to `dataset-mirror`.
   - Optional fast hash-only mode for CI (skip full re-hash when manifest already trusted).
4. Make training reproducible and resumable:
   - Deterministic file order from manifest.
   - Resume support via byte-range requests when CDN supports it (fallback to re-download with hash check).

**Implementation (single, correct, actionable)**

```bash
# /opt/axentx/vanguard/scripts/make_manifest.py
#!/usr/bin/env python3
"""
Generate content-addressed manifest for a date folder in a HuggingFace dataset repo.

Usage:
  HF_TOKEN=hf_xxx python make_manifest.py \
    --repo datasets/axentx/mirror-merged \
    --date 2026-05-03 \
    --out manifests/manifest-2026-05-03.json \
    --recursive \
    --skip-sha (fast CI mode)
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests

HF_API_BASE = "https://huggingface.co/api"
DEFAULT_RETRY_AFTER = 30
MAX_RETRIES = 5

def _wait_retry(resp: requests.Response, attempt: int) -> None:
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", DEFAULT_RETRY_AFTER))
        # Exponential-ish backoff with cap
        sleep_s = min(retry_after * (2 ** attempt), 3600)
        print(f"Rate limited. Retry after {sleep_s}s", file=sys.stderr)
        time.sleep(sleep_s)
        return
    resp.raise_for_status()

def list_date_files(repo: str, date_folder: str, token: Optional[str] = None, recursive: bool = True) -> List[Dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{HF_API_BASE}/datasets/{repo}/tree"
    params = {"path": date_folder, "recursive": "true" if recursive else "false"}
    for attempt in range(MAX_RETRIES):
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            _wait_retry(resp, attempt)
            continue
        resp.raise_for_status()
        items = resp.json()
        # Keep only files
        return [i for i in items if i.get("type") == "file"]
    raise RuntimeError("Max retries exceeded while listing repo tree")

def sha256_of_cdn_file(repo: str, filepath: str, token: Optional[str] = None) -> str:
    cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{filepath}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    h = hashlib.sha256()
    with requests.get(cdn_url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(
    repo: str,
    date_folder: str,
    out_path: Path,
    token: Optional[str] = None,
    recursive: bool = True,
    skip_sha: bool = False,
) -> None:
    files = list_date_files(repo, date_folder, token=token, recursive=recursive)
    if not files:
        print(f"No files found in {repo}/{date_folder}", file=sys.stderr)

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "recursive": recursive,
        "generated_by": "make_manifest.py",
        "files": []
    }

    for f in files:
        path = f["path"]
        entry = {"path": path, "size": f.get("size", 0)}
        if not skip_sha:
            print(f"Hashing {path}...", file=sys.stderr)
            entry["sha256"] = sha256_of_cdn_file(repo, path, token=token)
        manifest["files"].append(entry)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest['files'])} entries to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HF CDN manifest for a date folder.")
    parser.add_argument("--repo", required=True, help="e.g. datasets/axentx/mirror-merged")
    parser.add_argument("--date", required=True, help="Date folder under repo, e.g. 2026-05-03")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"), help="HF token (optional for public repos)")
    parser.add_argument("--recursive", action="store_true", default=True, help="List recursively (default True)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false")
    parser.add_argument("--skip-sha", action="store_true", help="Skip SHA256 computation (fast, less safe)")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, Path(args.out), token=args.token, recursive=args.recursive, skip_sha=args.skip_sha)
```

```python
# /opt/axentx/vanguard/train.py
"""
Lightning training entrypoint that uses a pre-generated manifest for CDN-only fetches.
Run on Mac to orchestrate Lightning Studio (never run heavy model.from_pretrained() locally).
"""
import json
import os
import sys
from pathlib import Path
from typing import Dict, Any

import lightning as L
import torch
from torch.utils.data import Dataset, DataLoader

# Optional studio helpers (keep lightweight)
def find_running_studio(name: str):
    try:
        from lightning.fabric.plugins.environments.lightning_environment import Teamspace
        for s in Teamspace.studios:
            if getattr(s, "name", None) == name and getattr(s, "status", None) == "Running":
                return s
    except Exception:
        pass
    return None

def project_schema(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enforce expected schema and dtypes.
    Replace with your actual projection logic.
    Returns dict with at least {prompt, response} as str.
    """
    # Placeholder: adapt to your data format (JSONL, parquet, etc.)
