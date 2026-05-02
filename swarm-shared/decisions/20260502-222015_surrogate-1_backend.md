# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What we’ll change (merged + corrected)
- Add `bin/list-files.py` — single Mac-side call that lists one date folder via `list_repo_tree(recursive=False)` and writes `file-list.json` (path + size + sha256).
- Add `tools/cdn_download.py` — CDN fetcher with retries, exponential backoff, and correct HF CDN URL formatting.
- Update `bin/dataset-enrich.sh` to accept optional `FILE_LIST`; if provided, iterate the list and download via CDN (zero API calls during ingestion).
- Update README with HF CDN bypass instructions.
- (Optional) Update `.github/workflows/ingest.yml` to pre-bake the file list once and pass it to all shard jobs.

**Why this is highest value**
- Eliminates 429s during ingestion/training (the biggest recurring failure mode).
- Cuts HF API calls to **one** per run (or zero if list is pre-baked).
- Keeps 16-shard parallelism intact while making it robust.
- Fits <2h: ~20 min list script, ~30 min CDN helper, ~40 min wiring into dataset-enrich.sh, ~20 min tests/docs.

---

## Code changes

### 1) `bin/list-files.py`
```python
#!/usr/bin/env python3
"""
List files in a single folder (non-recursive) for a HuggingFace dataset repo.
Usage:
  HF_TOKEN=<token> python bin/list-files.py \
    --repo axentx/surrogate-1-training-pairs \
    --path batches/public-merged/2026-05-02 \
    --output file-list.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser(description="List repo folder (non-recursive).")
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/repo)")
    parser.add_argument("--path", required=True, help="Folder path in repo")
    parser.add_argument("--output", required=True, help="Output JSON path")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token)

    try:
        tree = api.list_repo_tree(
            repo_id=args.repo,
            path=args.path,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as exc:
        print(f"ERROR listing repo tree: {exc}", file=sys.stderr)
        sys.exit(1)

    files = []
    for entry in tree:
        if entry.type == "file":
            files.append({
                "path": entry.path,
                "size": getattr(entry, "size", None),
                "sha256": getattr(entry, "sha256", None),
            })

    out = {
        "repo": args.repo,
        "path": args.path,
        "files": files,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"Wrote {len(files)} files to {args.output}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-files.py
```

---

### 2) `tools/cdn_download.py`
```python
#!/usr/bin/env python3
"""
Download a single file from HuggingFace datasets CDN (no auth header).
Retries with exponential backoff.
"""
import argparse
import sys
import time
from pathlib import Path

import requests

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def download_cdn(repo: str, path: str, dest: Path, max_retries: int = 5) -> None:
    # Normalize path separators for HF CDN (always forward slash)
    path = path.replace("\\", "/").lstrip("/")
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, max_retries + 1):
        try:
            # No Authorization header -> bypasses /api/ rate limits
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return
        except Exception as exc:
            wait = 2 ** attempt
            print(f"Attempt {attempt}/{max_retries} failed for {path}: {exc}. Retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)

    raise RuntimeError(f"Failed to download {path} after {max_retries} attempts")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args()
    download_cdn(args.repo, args.path, args.dest)
```

---

### 3) Update `bin/dataset-enrich.sh` (minimal, backward-compatible)
Add optional `FILE_LIST` support. If provided, use CDN downloads; otherwise keep existing behavior.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Optional: FILE_LIST=/path/to/file-list.json to enable CDN-only mode.

set -euo pipefail

REPO="${HF_DATASET_REPO:-axentx/surrogate-1-training-pairs}"
WORKDIR="${WORKDIR:-/tmp/surrogate-ingest}"
OUTPUT_DIR="${OUTPUT_DIR:-./output}"
FILE_LIST="${FILE_LIST:-}"

mkdir -p "$WORKDIR" "$OUTPUT_DIR"

if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
  echo "CDN-only mode: using file list $FILE_LIST"
  python3 - <<PY
import json, subprocess, sys, os
from pathlib import Path

with open("$FILE_LIST") as f:
    data = json.load(f)

repo = data["repo"]
workdir = Path("$WORKDIR")
workdir.mkdir(parents=True, exist_ok=True)

for item in data["files"]:
    path = item["path"]
    dest = workdir / Path(path).name
    # download via CDN (no auth)
    subprocess.run([
        sys.executable, "tools/cdn_download.py",
        "--repo", repo,
        "--path", path,
        "--dest", str(dest),
    ], check=True)

    # TODO: parse dest -> normalize -> dedup -> emit
    # Keep existing per-file parsing logic here.
    print(f"Processed {path}")
PY
else
  echo "Using existing streaming ingest (HF API path)"
  # existing python script or inline logic here
  python3 -m surrogate_ingest --repo "$REPO" --output "$OUTPUT_DIR"
fi
```

(If you prefer, I can inline the full Python parsing/dedup logic into the script instead of the stub.)

---

### 4) Update `.github/workflows/ingest.yml` (optional but recommended)
Allow passing the pre-baked file list into the matrix job so shards use CDN-only paths.

```yaml
# Add to job steps (example)
- name: Prepare file list (once per workflow)
  if: matrix.shard_id == 0
  run: |
    python bin/list-files.py \
      --repo axentx/surrogate-1-training-pairs \
      --path "batches/public-merged/$(date +%Y-%m-%d)" \
      --output file-list.json

- name: Run shard with file list
  env:
    FILE_LIST: ${{ github.workspace }}/file-list.json
  run: |
    bin/dataset-enrich.sh
```

---

### 5) README update (short)
Add a “HF CDN bypass” section:

```markdown
## HF CDN bypass (recommended)

To avoid HF API rate limits during ingestion and training:

1. On your Mac (or any trusted machine), generate a file list:
   ```bash
   HF_TOKEN=<token> python bin/list-files.py \
     --repo axentx/surrogate
