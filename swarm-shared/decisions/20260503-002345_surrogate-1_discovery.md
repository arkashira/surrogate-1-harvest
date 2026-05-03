# surrogate-1 / discovery

## Final Implementation (merged & hardened)

**Core deliverable**: a deterministic `snapshot.json` per date folder + a CDN-first loader that eliminates HF API calls during training, preventing 429s while keeping ingestion unchanged.

---

### 1) Create `bin/snapshot.sh` (15 min)

Deterministic manifest for one date folder via a **single non-recursive tree call**.

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... ./bin/snapshot.sh <date> [out.json]
# Example: ./bin/snapshot.sh 2026-05-03 snapshots/public-merged/2026-05-03/manifest.json

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="${2:-snapshots/public-merged/${DATE}/manifest.json}"
HF_TOKEN="${HF_TOKEN:-}"

if [ -z "$HF_TOKEN" ]; then
  echo "ERROR: HF_TOKEN is required" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"

python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
date = sys.argv[2]
out_path = sys.argv[3]
token = os.environ["HF_TOKEN"]

api = HfApi(token=token)

# Single non-recursive call
tree = api.list_repo_tree(
    repo_id=repo,
    path=f"batches/public-merged/{date}",
    repo_type="dataset",
    recursive=False
)

manifest = []
for item in tree:
    if item.path.rstrip("/").endswith("/"):
        continue  # skip directory entries
    manifest.append({
        "path": item.path,
        "size": getattr(item, "size", None),
        "lfs": getattr(item, "lfs", None),
    })

# Deterministic ordering
manifest.sort(key=lambda x: x["path"])

with open(out_path, "w", encoding="utf-8") as f:
    json.dump({"repo": repo, "date": date, "files": manifest}, f, indent=2, sort_keys=True)

print(f"Wrote {len(manifest)} entries to {out_path}")
PY
```

---

### 2) Add `bin/lib/cdn_loader.py` (15 min)

Streaming CDN loader with retries, integrity checks, and deterministic URL builder.

```python
# bin/lib/cdn_loader.py
import json
import time
import hashlib
from pathlib import Path
from typing import Iterator, Dict, Any, Optional

import requests
from requests.adapters import HTTPAdapter, Retry

CDN_ROOT = "https://huggingface.co/datasets"

def cdn_url(repo: str, path: str) -> str:
    return f"{CDN_ROOT}/{repo}/resolve/main/{path}"

def _make_session() -> requests.Session:
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist={429, 500, 502, 503, 504},
        allowed_methods={"GET", "HEAD"},
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

_SESSION = _make_session()

def stream_jsonl_cdn(
    repo: str,
    filepath: str,
    expected_size: Optional[int] = None,
    chunk_size: int = 8192,
) -> Iterator[Dict[str, Any]]:
    url = cdn_url(repo, filepath)
    with _SESSION.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        if expected_size is not None:
            got = int(r.headers.get("content-length", 0))
            if got != expected_size:
                raise ValueError(f"Size mismatch for {filepath}: expected {expected_size}, got {got}")

        buffer = ""
        for chunk in r.iter_content(chunk_size=chunk_size, decode_unicode=True):
            if not chunk:
                continue
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if line:
                    yield json.loads(line)

def load_manifest(manifest_path: str) -> Dict[str, Any]:
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)
```

---

### 3) Update training script (30–40 min)

Accept `--snapshot`, stream via CDN, fall back to legacy `load_dataset` when no snapshot.

```python
# train.py additions
import argparse
import random
from pathlib import Path

from bin.lib.cdn_loader import load_manifest, stream_jsonl_cdn

def build_dataloader_from_snapshot(manifest_path: str, repo: str, seed: int = 42):
    manifest = load_manifest(manifest_path)
    files = sorted(manifest["files"], key=lambda x: x["path"])
    random.Random(seed).shuffle(files)

    def generator():
        for item in files:
            try:
                for record in stream_jsonl_cdn(
                    repo=repo,
                    filepath=item["path"],
                    expected_size=item.get("size"),
                ):
                    yield {
                        "prompt": record.get("prompt") or record.get("input") or "",
                        "response": record.get("response") or record.get("output") or "",
                    }
            except Exception as exc:
                # Log and skip bad shards instead of crashing training
                print(f"Skipping {item['path']}: {exc}")
                continue

    return generator

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=str, default=None, help="Path to manifest.json")
    parser.add_argument("--repo", type=str, default="axentx/surrogate-1-training-pairs")
    args = parser.parse_args()

    if args.snapshot:
        gen = build_dataloader_from_snapshot(args.snapshot, args.repo)
        # Example: consume first batch
        for i, item in enumerate(gen):
            if i >= 10:
                break
            print(item)
    else:
        # Legacy path (unchanged)
        from datasets import load_dataset
        ds = load_dataset(args.repo, split="train")
        print("Legacy dataset loaded (no snapshot).")
```

---

### 4) (Optional) GitHub Actions: daily snapshot

Add a lightweight workflow to generate a snapshot once per day and commit it to the repo (or upload as artifact).

```yaml
# .github/workflows/daily-snapshot.yml
name: Daily snapshot

on:
  schedule:
    - cron: "0 3 * * *"   # 03:00 UTC daily
  workflow_dispatch:

jobs:
  snapshot:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: pip install -r requirements.txt

      - name: Generate snapshot
        run: |
          DATE=$(date -u +%Y-%m-%d)
          mkdir -p snapshots/public-merged/${DATE}
          HF_TOKEN=${{ secrets.HF_TOKEN }} \
            ./bin/snapshot.sh "${DATE}" "snapshots/public-merged/${DATE}/manifest.json"

      - name: Upload snapshot artifact
        uses: actions/upload-artifact@v4
        with:
          name: snapshot-${{ github.run_id }}
          path: snapshots/
```

---

### 5) Verification checklist (20 min)

1. Generate snapshot:
   ```bash
   HF_TOKEN=... ./bin/snapshot.sh 2026-05-03 snapshots/public-merged/2026-05-03/manifest.json
   ```
   - Confirm `manifest.json` exists and contains deterministic file list.

2. Run training with snapshot:
   ```bash
   python train.py --snapshot snapshots/public-merged/2026-05-03/manifest.json
   ```
   - Confirm logs show CDN URLs (`resolve/main/...`) and **no** `/api/` calls
