# vanguard / quality

### Final synthesized solution (correct + actionable)

**Core diagnosis (merged, de-duplicated)**
- Authenticated `list_repo_tree` is executed repeatedly (frontend, training reloads, per-worker) and burns the 1000/5 min quota, causing 429s.
- No persisted `(repo, dateFolder) → file-list` manifest exists, so every session re-enumerates and risks authenticated fetches.
- Training/data-loading code can still trigger authenticated calls (via SDK streaming or proxy APIs), violating the CDN-bypass pattern.
- There is no lightweight orchestration to pre-list once, write a manifest, and enforce CDN-only fetches during training.

**Single proposed change (high-leverage, minimal)**
Add one CLI script that:
1. Performs **one** authenticated `list_repo_tree` (non-recursive) for `(repo, dateFolder)`.
2. Persists a manifest with repo, dateFolder, sorted file list, and `cdn_base`.
3. Is idempotent and outputs the manifest path for CI/launcher consumption.

Update training launcher to:
- Require the manifest before training starts (fail fast if missing).
- Use only CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{dateFolder}/{file}`) during data loading; no SDK dataset streaming, no authenticated API calls.

**Implementation (merged best parts, concrete and correct)**

1) Create manifest builder (single source of truth)

```bash
mkdir -p /opt/axentx/vanguard/scripts /opt/axentx/vanguard/manifests
```

File: `/opt/axentx/vanguard/scripts/build_file_manifest.py`

```python
#!/usr/bin/env python3
"""
build_file_manifest.py
Generate a persisted (repo, date_folder) -> file-list manifest for CDN-only training.

Usage:
  HF_TOKEN=hf_xxx python3 build_file_manifest.py \
    --repo datasets/MyOrg/vanguard-data \
    --date-folder 2026-05-03 \
    --out-dir ../../manifests

Training must use the manifest and fetch via CDN URLs only:
  https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{file}
(no authenticated API calls during data loading).
"""

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("ERROR: huggingface_hub not installed. Install with: pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

def main() -> None:
    parser = argparse.ArgumentParser(description="Build file manifest for CDN-only training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/MyOrg/vanguard-data)")
    parser.add_argument("--date-folder", required=True, help="Folder under repo to list (non-recursive).")
    parser.add_argument("--out-dir", default="../../manifests", help="Output directory for manifest.")
    args = parser.parse_args()

    api = HfApi(token=os.getenv("HF_TOKEN"))
    repo = args.repo.strip("/")
    path = args.date_folder.strip("/")

    # One authenticated API call (non-recursive)
    try:
        items = api.list_repo_tree(repo=repo, path=path, recursive=False)
    except Exception as exc:
        print(f"ERROR: Failed to list repo tree for {repo}/{path}: {exc}", file=sys.stderr)
        sys.exit(1)

    files = sorted(item.rfilename for item in items if item.rfilename)
    if not files:
        print(f"WARN: No files found at {repo}/{path}", file=sys.stderr)

    manifest = {
        "repo": repo,
        "date_folder": path,
        "files": files,
        "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main/{path}",
        "generated_by": "build_file_manifest.py",
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    slug = repo.replace("/", "_")
    out_path = out_dir / f"{slug}__{path}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(str(out_path))

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/scripts/build_file_manifest.py
```

2) Launcher guard + training contract (enforce CDN-only)

Update your launcher (or create `run_training.sh`) to require the manifest and pass it to training:

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="datasets/MyOrg/vanguard-data"
DATE_FOLDER="2026-05-03"
MANIFEST="/opt/axentx/vanguard/manifests/datasets_MyOrg_vanguard-data__${DATE_FOLDER}.json"

if [ ! -f "$MANIFEST" ]; then
  HF_TOKEN="${HF_TOKEN:?HF_TOKEN required}" \
    python3 /opt/axentx/vanguard/scripts/build_file_manifest.py \
      --repo "$REPO" \
      --date-folder "$DATE_FOLDER" \
      --out-dir /opt/axentx/vanguard/manifests
fi

# Training must use manifest and CDN-only URLs (no authenticated API calls during data loading)
exec python3 /opt/axentx/vanguard/train.py --manifest "$MANIFEST" "$@"
```

3) Training/data loader change (CDN-only, no SDK streaming)

In `train.py` (or data module), consume the manifest and fetch via CDN:

```python
import json
import requests
from pathlib import Path
from typing import List, Dict

def load_manifest(manifest_path: str) -> Dict:
    with open(manifest_path) as f:
        return json.load(f)

def stream_files_from_manifest(manifest: Dict):
    base = manifest["cdn_base"].rstrip("/")
    for fname in manifest["files"]:
        url = f"{base}/{fname}"
        # Streaming download to avoid large memory usage
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            # Example: yield raw bytes or parse in-memory
            # For parquet/jsonl/etc., parse from r.raw or r.content as needed
            yield fname, r.content

# Example usage in training setup
def prepare_dataset(manifest_path: str):
    manifest = load_manifest(manifest_path)
    examples = []
    for fname, content in stream_files_from_manifest(manifest):
        # Replace with actual parsing (e.g., parquet, jsonl, image decode)
        examples.append({"file": fname, "content": content})
    return examples
```

**Verification (clear pass/fail checks)**

1. Generate manifest once:
   ```bash
   HF_TOKEN=hf_xxx python3 /opt/axentx/vanguard/scripts/build_file_manifest.py \
     --repo datasets/MyOrg/vanguard-data \
     --date-folder 2026-05-03 \
     --out-dir /opt/axentx/vanguard/manifests
   ```
   - Confirm JSON exists and contains non-empty `files` and correct `cdn_base`.

2. Confirm training uses only CDN URLs:
   - Run training with the manifest.
   - Monitor outbound requests (e.g., via logs or `tcpdump`/`mitmproxy`).
   - Verify **zero** authenticated calls to `/api/` or `list_repo_tree` during data loading.
   - Verify all file fetches are to `https://huggingface.co/datasets/.../resolve/main/...`.

3. Quota/429 check:
   - Re-run training multiple times (reload workers, restart script).
   - Confirm no increase in authenticated API calls and no 429s attributable to listing.

4. Correctness check:
   - Confirm training completes and processes the expected number of files (matches manifest length).

**Notes for production rollout**
- Keep the manifest in version control or artifact store if reproducibility is required.
- If files change under `(repo, dateFolder)`, regenerate the manifest (the script is idempotent).
- For large folders, consider sharding or parallel CDN downloads inside `stream_files_from_manifest`, but keep the pattern CDN-only and manifest-driven.
