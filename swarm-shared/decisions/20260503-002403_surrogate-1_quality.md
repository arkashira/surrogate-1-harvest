# surrogate-1 / quality

## Implementation Plan — CDN-first snapshot + zero-HF-API ingestion

**Goal**: Eliminate HF API rate-limit risk during training by producing a deterministic file manifest once (on the Mac orchestrator) and having Lightning training fetch exclusively via CDN URLs.

**Scope** (≤2h):
1. Add `bin/snapshot.sh` — lists one date folder via HF API once, saves `snapshot.json` with CDN URLs + metadata.
2. Add `bin/build_manifest.py` — deterministic ordering, slug → CDN URL, size, etag.
3. Update training entrypoint to accept `--manifest snapshot.json` and use `requests.get(cdn_url)` or `wget` in the data loader (no `load_dataset`/`list_repo_files` during training).
4. Reuse running Lightning Studio if present; restart only if stopped.

---

### 1) `bin/snapshot.sh`

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... ./bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-05-03
set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE="${2:-$(date +%Y-%m-%d)}"
OUTDIR="snapshots/${DATE}"
MANIFEST="${OUTDIR}/snapshot.json"

mkdir -p "${OUTDIR}"

echo "Listing ${REPO} tree for date=${DATE} ..."
# Single API call; recursive=False to avoid pagination explosion
python3 - <<PY > "${OUTDIR}/tree.json"
import os, json, sys
from huggingface_hub import HfApi
api = HfApi(token=os.environ.get("HF_TOKEN"))
repo = sys.argv[1]
date = sys.argv[2]
items = api.list_repo_tree(repo, path=date, recursive=False)
# Keep only files (exclude subfolders)
files = [it for it in items if it.type == "file"]
print(json.dumps([{"path": f.path, "size": getattr(f, "size", None)} for f in files], indent=2))
PY "$REPO" "$DATE"

echo "Building manifest with CDN URLs ..."
python3 bin/build_manifest.py "${OUTDIR}/tree.json" "$REPO" "$DATE" > "$MANIFEST"
echo "Snapshot written to $MANIFEST"
```

Make executable:

```bash
chmod +x bin/snapshot.sh
```

---

### 2) `bin/build_manifest.py`

```python
#!/usr/bin/env python3
# bin/build_manifest.py
# Usage: build_manifest.py tree.json repo date > snapshot.json
import json, sys, hashlib, os

def build_manifest(tree_path: str, repo: str, date: str):
    with open(tree_path) as f:
        tree = json.load(f)

    # Deterministic ordering
    entries = sorted(tree, key=lambda x: x["path"])

    base = f"https://huggingface.co/datasets/{repo}/resolve/main"
    manifest = {
        "repo": repo,
        "date": date,
        "generated_by": "bin/snapshot.sh",
        "cdn_base": base,
        "files": [],
    }

    for item in entries:
        path = item["path"]
        slug = os.path.splitext(os.path.basename(path))[0]
        cdn_url = f"{base}/{path}"
        manifest["files"].append(
            {
                "slug": slug,
                "path": path,
                "cdn_url": cdn_url,
                "size": item.get("size"),
                # lightweight content-addressable hint (not authoritative)
                "hint": hashlib.sha256(path.encode()).hexdigest()[:16],
            }
        )

    manifest["count"] = len(manifest["files"])
    print(json.dumps(manifest, indent=2))

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: build_manifest.py tree.json repo date", file=sys.argv)
        sys.exit(1)
    build_manifest(sys.argv[1], sys.argv[2], sys.argv[3])
```

Make executable:

```bash
chmod +x bin/build_manifest.py
```

---

### 3) Training loader using CDN-only (minimal diff)

Add to your training script (or create `data/cdn_loader.py`):

```python
# data/cdn_loader.py
import json, io, pyarrow as pa, pyarrow.parquet as pq, requests
from torch.utils.data import IterableDataset

class CDNParquetPairs(IterableDataset):
    def __init__(self, manifest_path, columns=("prompt", "response")):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.columns = columns

    def _stream_file(self, cdn_url):
        # CDN fetch — no Authorization header, bypasses HF API rate limits
        resp = requests.get(cdn_url, timeout=60)
        resp.raise_for_status()
        return io.BytesIO(resp.content)

    def __iter__(self):
        for f in self.manifest["files"]:
            try:
                buf = self._stream_file(f["cdn_url"])
                table = pq.read_table(buf, columns=self.columns)
                for row in table.to_pylist():
                    yield row
            except Exception as exc:
                # Log and skip bad files; don't crash entire epoch
                print(f"Skipping {f['path']}: {exc}")
                continue
```

Usage in training launcher:

```python
from data.cdn_loader import CDNParquetPairs
from lightning import Fabric

fabric = Fabric()
train_ds = CDNParquetPairs("snapshots/2026-05-03/snapshot.json")
train_dl = torch.utils.data.DataLoader(train_ds, batch_size=8, num_workers=4)
train_dl = fabric.setup_dataloaders(train_dl)
```

---

### 4) Lightning Studio reuse + safe restart

Wrap training launch with reuse logic (saves quota):

```python
# launch_studio.py
import os
from lightning import Studio, Teamspace, Machine

STUDIO_NAME = "surrogate-1-train"
MACHINE = Machine.L40S  # or priority order: try H200 in lightning-lambda-prod if quota

teamspace = Teamspace()
running = None
for s in teamspace.studios:
    if s.name == STUDIO_NAME and s.status == "Running":
        running = s
        break

if running:
    print(f"Reusing running studio: {running.name}")
    studio = running
else:
    print(f"Starting new studio: {STUDIO_NAME}")
    studio = Studio(
        name=STUDIO_NAME,
        machine=MACHINE,
        create_ok=True,
    )

# Ensure studio is running before .run()
if studio.status != "Running":
    print("Studio not running; starting...")
    studio.start(machine=MACHINE)

# Run training with CDN manifest
studio.run(
    "train.py",
    arguments=[
        "--manifest", "snapshots/2026-05-03/snapshot.json",
        "--output-dir", "outputs/run-001",
    ],
    cwd="/workspace",
)
```

---

### 5) Quick test (local)

```bash
# 1) Produce snapshot (single API call)
HF_TOKEN=hf_... ./bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-05-03

# 2) Dry-run loader
python3 -c "from data.cdn_loader import CDNParquetPairs; ds = CDNParquetPairs('snapshots/2026-05-03/snapshot.json'); print(next(iter(ds)))"
```

---

### 6) Rollout checklist

- [ ] `chmod +x bin/snapshot.sh bin/build_manifest.py`
- [ ] Add `snapshots/` to `.gitignore` (store manifests locally or in CI artifacts).
- [ ] Update training entrypoint to accept `--manifest` and use `CDNParquetPairs`.
- [ ] Replace any `load_dataset(..., streaming=True)` calls with CDN loader for this repo.
- [ ] Verify Lightning quota: reuse running studio; fallback to L40S if H200 unavailable.
- [ ] CI: keep existing 16-shard `ingest.yml` unchanged (it still uses HF API + dedup); this change only affects training.

**Impact**: Training no longer
