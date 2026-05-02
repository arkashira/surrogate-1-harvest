# airship / discovery

## Final Implementation Plan — Deterministic CDN-Only `airship discover`

**Highest-value improvement**: Harden `airship discover` into a **deterministic, CDN-only, zero-HF-API-runtime orchestrator** that eliminates HF API rate limits and PyArrow schema errors while producing a content-addressed staging area and a CDN-resolvable manifest.

---

### 1) CLI entrypoint (`/opt/axentx/airship/airship`)

```bash
#!/usr/bin/env bash
# /opt/axentx/airship/airship
set -euo pipefail
SHELL=/bin/bash

AXENTX_ROOT="/opt/axentx/airship"
export AXENTX_ROOT

cmd="${1:-help}"
shift || true

case "$cmd" in
  discover)
    exec "$AXENTX_ROOT/scripts/discover/run.sh" "$@"
    ;;
  *)
    echo "Usage: $0 {discover} [date-folder]"
    exit 1
    ;;
esac
```

```bash
chmod +x /opt/axentx/airship/airship
```

---

### 2) Discover orchestrator (`scripts/discover/run.sh`)

```bash
#!/usr/bin/env bash
# /opt/axentx/airship/scripts/discover/run.sh
# Deterministic, CDN-only discovery. Zero HF API calls during runtime.
set -euo pipefail
SHELL=/bin/bash

AXENTX_ROOT="${AXENTX_ROOT:-/opt/axentx/airship}"
HF_REPO="${HF_REPO:-datasets/axentx/surrogate-mirror}"
DATE_FOLDER="${1:-$(date -u +%Y-%m-%d)}"
OUTDIR="${AXENTX_ROOT}/staging/discover/${DATE_FOLDER}"
MANIFEST="${OUTDIR}/manifest.json"

mkdir -p "${OUTDIR}"

echo "== airship discover =="
echo "HF_REPO=${HF_REPO}"
echo "DATE_FOLDER=${DATE_FOLDER}"
echo "OUTDIR=${OUTDIR}"

# Step 1: Single API call (safe window) to list one date folder (non-recursive)
# This avoids recursive list_repo_files and 429/128-commit caps.
echo "[1/4] Listing folder (single API call)..."
python3 "${AXENTX_ROOT}/scripts/discover/list_folder.py" \
  --repo "${HF_REPO}" \
  --path "${DATE_FOLDER}" \
  --out "${OUTDIR}/file_list.json"

# Step 2: CDN-only fetch manifest + selected parquet shards
# Uses public CDN URLs (no Authorization header) → bypasses /api/ rate limits.
echo "[2/4] Fetching via CDN (zero HF API)..."
python3 "${AXENTX_ROOT}/scripts/discover/fetch_cdn.py" \
  --repo "${HF_REPO}" \
  --file-list "${OUTDIR}/file_list.json" \
  --outdir "${OUTDIR}"

# Step 3: Lightweight projection to {prompt,response} only
# Avoids load_dataset(streaming=True) on mixed-schema repos → prevents PyArrow CastError.
echo "[3/4] Projecting to {prompt,response}..."
python3 "${AXENTX_ROOT}/scripts/discover/project.py" \
  --indir "${OUTDIR}" \
  --outdir "${OUTDIR}/projected"

# Step 4: Content-addressed manifest (CDN-resolvable)
echo "[4/4] Building manifest..."
python3 "${AXENTX_ROOT}/scripts/discover/manifest.py" \
  --indir "${OUTDIR}/projected" \
  --date "${DATE_FOLDER}" \
  --out "${MANIFEST}"

echo "Done. Manifest: ${MANIFEST}"
echo "CDN base: https://huggingface.co/datasets/${HF_REPO}/resolve/main/${DATE_FOLDER}/"
```

```bash
chmod +x /opt/axentx/airship/scripts/discover/run.sh
```

---

### 3) List folder (single API call) (`scripts/discover/list_folder.py`)

```python
#!/usr/bin/env python3
# /opt/axentx/airship/scripts/discover/list_folder.py
# Single list_repo_tree(path, recursive=False) → avoids recursive pagination & rate limits.
import argparse
import json
import os
from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi()
    # Non-recursive: one page, no 100× pagination, no 429 from massive listing.
    tree = api.list_repo_tree(repo_id=args.repo, path=args.path, recursive=False)

    files = []
    for entry in tree:
        if entry.type == "file":
            # Only pick parquet for surrogate training (avoid schema heterogeneity).
            if entry.path.endswith(".parquet"):
                files.append(
                    {
                        "path": entry.path,
                        "size": getattr(entry, "size", None),
                    }
                )

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"repo": args.repo, "folder": args.path, "files": files}, f, indent=2)

    print(f"Listed {len(files)} parquet files to {args.out}")

if __name__ == "__main__":
    main()
```

---

### 4) CDN-only fetch (`scripts/discover/fetch_cdn.py`)

```python
#!/usr/bin/env python3
# /opt/axentx/airship/scripts/discover/fetch_cdn.py
# Downloads via https://huggingface.co/datasets/{repo}/resolve/main/{path}
# No Authorization header → bypasses /api/ rate limits entirely.
import argparse
import json
import os
import requests
import hashlib
import time
from urllib.parse import quote

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def cdn_url(repo: str, path: str) -> str:
    return CDN_TEMPLATE.format(repo=repo, path=quote(path))

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--file-list", required=True)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()

    with open(args.file_list) as f:
        listing = json.load(f)

    os.makedirs(args.outdir, exist_ok=True)

    for item in listing["files"]:
        rel = item["path"]
        name = os.path.basename(rel)
        out_path = os.path.join(args.outdir, name)

        if os.path.exists(out_path):
            print(f"Skip (exists): {name}")
            continue

        url = cdn_url(args.repo, rel)
        print(f"Fetching: {name}")
        try:
            resp = requests.get(url, timeout=120, stream=True)
            resp.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            item["sha256"] = sha256_file(out_path)
            print(f"  OK {item['sha256'][:12]}...")
        except Exception as exc:
            # If 429 on CDN is ever hit, wait and retry (CDN limits >> API limits).
            print(f"  ERROR: {exc}")
            if os.path.exists(out_path):
                os.unlink(out_path)
            # Basic retry for transient CDN errors
            time.sleep(5)
            continue

    # Update file list with checksums
    with open(args.file_list, "w") as f:
        json.dump(listing, f, indent=2)

    print("CDN fetch complete.")

if __name__ == "__main__":
    main()
```

---

### 5) Projection to `{prompt,response}` (`scripts/discover/project.py`)

```python
#!/usr/bin/env python3
# /opt/axent
