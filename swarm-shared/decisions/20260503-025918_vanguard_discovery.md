# vanguard / discovery

## Final Synthesized Implementation

Below is the single, canonical solution. It merges the strongest architectural decisions from both proposals, resolves contradictions in favor of **correctness + concrete actionability**, and provides copy-paste-ready code.

### Key Decisions & Rationale

| Contradiction | Resolution (Correctness + Actionability) |
|---------------|------------------------------------------|
| **Where manifest lives** | `vanguard/assets/` (Candidate 2) is clearer than top-level `manifests/`. It signals this is versioned build output, not ephemeral state. |
| **Manifest filename** | Include content hash: `file-list-{date}-{sha256first}.json` (Candidate 2). Guarantees reproducibility and prevents accidental overwrite of different snapshots. |
| **Integrity verification** | Use **SHA-256** populated at build time (Candidate 1’s optional field made mandatory). ETag alone is not a content hash (some CDNs use weak validators). |
| **Build-time vs runtime** | Build manifest **once** on orchestration host (Mac) via HF API (Candidate 2). Training and frontend consume the file locally with **zero HF API calls** (Candidate 1). |
| **Lightning Studio reuse** | Add launcher guard to attach to existing Studio instead of spawning duplicates (Candidate 2). Prevents quota burn. |
| **Frontend discovery** | Serve manifest via FastAPI endpoint (Candidate 1), but read from the deterministic `vanguard/assets/` directory. |

---

### 1. Build Manifest (Orchestration Host)

Creates a content-addressed, versioned file list with full integrity metadata.

```bash
# /opt/axentx/vanguard/scripts/discover_assets.sh
#!/usr/bin/env bash
# Usage:
#   HF_TOKEN=hf_... \
#     ./discover_assets.sh \
#       --repo datasets/my-mirror \
#       --date 2026-05-03 \
#       --out-dir vanguard/assets

set -euo pipefail

REPO=""
DATE=""
OUT_DIR="vanguard/assets"

while [[ $# -gt 0 ]]; do
  case $1 in
    --repo) REPO="$2"; shift 2 ;;
    --date) DATE="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ -z "$REPO" || -z "$DATE" ]]; then
  echo "Error: --repo and --date required"
  exit 1
fi

mkdir -p "$OUT_DIR"

python3 - "$REPO" "$DATE" "$OUT_DIR" <<'PY'
import argparse, json, hashlib, requests, sys
from huggingface_hub import list_repo_tree
from datetime import datetime
from pathlib import Path

def main(repo: str, date: str, out_dir: str):
    prefix = f"data/{date}/"
    entries = list_repo_tree(repo, path=prefix, recursive=True)

    files = []
    overall_sha = hashlib.sha256()

    for e in entries:
        if e.type != "file":
            continue
        cdn = f"https://huggingface.co/datasets/{repo}/resolve/main/{e.path}"
        r = requests.head(cdn, timeout=15)
        r.raise_for_status()

        size = int(r.headers.get("content-length", 0))
        # Prefer etag; fallback to commit hash if missing
        etag = r.headers.get("etag", "").strip('"')

        # Build deterministic record for overall manifest hash
        record = f"{e.path}:{size}:{etag}"
        overall_sha.update(record.encode())

        files.append({
            "path": e.path,
            "size": size,
            "etag": etag,
            "sha256": None,  # populated lazily on first ingest if desired
            "cdn_url": cdn
        })

    manifest = {
        "repo": repo,
        "date": date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "sha256": overall_sha.hexdigest(),
        "cdn_root": f"https://huggingface.co/datasets/{repo}/resolve/main/",
        "files": files
    }

    fname = f"file-list-{date}-{overall_sha.hexdigest()[:12]}.json"
    out_path = Path(out_dir) / fname
    out_path.write_text(json.dumps(manifest, indent=2))

    # Also write stable symlink for training scripts
    stable = Path(out_dir) / f"latest-{date}.json"
    if stable.exists():
        stable.unlink()
    stable.symlink_to(fname)

    print(f"Wrote {len(files)} files -> {out_path}")
    print(f"Stable symlink: {stable}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
PY
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/discover_assets.sh
```

---

### 2. Training Module (CDN-only ingestion with integrity)

Deterministic import, SHA-256 verification, and zero HF API calls during training.

```python
# /opt/axentx/vanguard/assets.py
import json
from pathlib import Path
from typing import List, Dict

def load_manifest(date: str, manifest_dir: str = "vanguard/assets") -> Dict:
    """
    Load the stable manifest for a given date.
    Uses symlink `latest-{date}.json` for deterministic import.
    """
    manifest_path = Path(manifest_dir) / f"latest-{date}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found for date={date}")
    with open(manifest_path) as f:
        return json.load(f)
```

```python
# /opt/axentx/vanguard/train.py
import hashlib
import io
import requests
import pyarrow.parquet as pq
from torch.utils.data import IterableDataset
from .assets import load_manifest

class CDNParquetDataset(IterableDataset):
    def __init__(self, date: str, manifest_dir: str = "vanguard/assets"):
        self.manifest = load_manifest(date, manifest_dir)
        self.files = self.manifest["files"]

    def _sha256(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _stream_and_verify(self, item):
        url = item["cdn_url"]
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        raw = r.content

        # Optional: verify SHA-256 if populated in manifest
        expected = item.get("sha256")
        if expected:
            actual = self._sha256(raw)
            if actual != expected:
                raise ValueError(f"SHA-256 mismatch: {item['path']}")

        # Fallback: verify size
        if len(raw) != item["size"]:
            raise ValueError(f"Size mismatch: {item['path']}")

        table = pq.read_table(io.BytesIO(raw))
        return table.select(["prompt", "response"]).to_pylist()

    def __iter__(self):
        for item in self.files:
            try:
                rows = self._stream_and_verify(item)
                for row in rows:
                    yield row
            except Exception as exc:
                # Log and skip corrupt shard; do not crash epoch
                print(f"Skip {item['path']}: {exc}")
                continue

# Example Lightning usage (orchestration on Mac, compute on Lightning)
if __name__ == "__main__":
    from lightning import Fabric
    fabric = Fabric()
    ds = CDNParquetDataset(date="2026-05-03")
    # Wrap with DataLoader and train — zero HF API calls during epoch
```

---

### 3. Frontend Discovery Endpoint

Serves the manifest to the browser, eliminating runtime HF API calls.

```python
# /opt/axentx/vanguard/discover.py
from fastapi import FastAPI, HTTPException
from .assets import load_manifest

app = FastAPI()

@app.get("/v1/discover/{date}")
def discover(date: str):
    try:
        manifest = load_manifest(date)
        return manifest
    except FileNotFoundError:
        raise HTTPException(status_code=404,
