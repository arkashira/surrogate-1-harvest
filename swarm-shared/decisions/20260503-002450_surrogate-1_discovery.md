# surrogate-1 / discovery

## Implementation Plan — CDN-first snapshot + zero-HF-API ingestion

**Goal**: Eliminate HF API rate-limit risk during training by producing a deterministic file manifest once (on the Mac orchestrator) and having Lightning training fetch exclusively via CDN URLs.

**Scope**: Add `bin/snapshot.sh` + tiny Python helpers to embed file lists and use CDN-only fetches during training. No changes to GitHub Actions runners (they already stream via CDN).

---

### 1) Add snapshot script (Mac orchestrator)

`bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# Usage: HF_TOKEN=... ./bin/snapshot.sh <repo> <date-folder>
# Example: HF_TOKEN=... ./bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-05-03
#
# Produces:
#   snapshots/<repo-slug>/<date-folder>/files.json
#   snapshots/<repo-slug>/<date-folder>/cdn_urls.txt
#
# Notes:
# - Uses HF API only once per snapshot (list_repo_tree).
# - CDN URLs are unsigned and bypass /api/ rate limits.
# - Deterministic ordering (sorted by path).

set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE="${2:-$(date +%Y-%m-%d)}"
OUTDIR="snapshots/$(echo "$REPO" | tr '/' '-')/$DATE"
mkdir -p "$OUTDIR"

echo "[snapshot] listing $REPO/$DATE (non-recursive)"
python3 - "$REPO" "$DATE" "$OUTDIR" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
date = sys.argv[2]
outdir = sys.argv[3]

api = HfApi()
# List only immediate children under date folder
tree = api.list_repo_tree(repo=repo, path=date, recursive=False)

files = []
for item in tree:
    if item.type == "file":
        files.append(item.path)

# Deterministic ordering
files.sort()

os.makedirs(outdir, exist_ok=True)

with open(os.path.join(outdir, "files.json"), "w") as f:
    json.dump({"repo": repo, "date": date, "files": files}, f, indent=2)

cdn_base = f"https://huggingface.co/datasets/{repo}/resolve/main"
with open(os.path.join(outdir, "cdn_urls.txt"), "w") as f:
    for p in files:
        f.write(f"{cdn_base}/{p}\n")

print(f"[snapshot] {len(files)} files -> {outdir}")
PY

echo "[snapshot] done"
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

### 2) Add lightweight loader for training (CDN-only)

`bin/cdn_loader.py`
```python
#!/usr/bin/env python3
"""
CDN-only loader for surrogate-1 training pairs.

Usage:
    python bin/cdn_loader.py snapshots/axentx-surrogate-1-training-pairs/2026-05-03/cdn_urls.txt

Behavior:
- Reads CDN URLs from text file (one per line).
- Downloads each file via requests (streaming) and yields {prompt, response}.
- No HuggingFace `load_dataset` or `hf_hub_download` calls during training loop.
- Avoids HF API entirely after snapshot.
"""
import json
import sys
from pathlib import Path
from typing import Iterator, Dict

import requests
import pyarrow.parquet as pq
from pyarrow import Table

CDN_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB


def parquet_to_pairs(table: Table) -> Iterator[Dict[str, str]]:
    """Project table rows to {prompt, response}. Tolerant to extra columns."""
    df = table.to_pandas()
    for _, row in df.iterrows():
        prompt = row.get("prompt") or row.get("input") or ""
        response = row.get("response") or row.get("output") or ""
        if prompt or response:
            yield {"prompt": str(prompt), "response": str(response)}


def stream_cdn_parquet(url: str) -> Iterator[Dict[str, str]]:
    """Download parquet via CDN URL and yield pairs without touching HF API."""
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()

    # Download to temp bytes (small parquet files expected)
    data = resp.content
    table = pq.read_table(pq.ParquetFile(pq.ParquetInputFile(pq.BufferReader(data))))
    yield from parquet_to_pairs(table)


def load_from_manifest(manifest_path: Path) -> Iterator[Dict[str, str]]:
    urls = [line.strip() for line in manifest_path.read_text().splitlines() if line.strip()]
    for url in urls:
        try:
            yield from stream_cdn_parquet(url)
        except Exception as exc:
            print(f"[cdn_loader] WARN failed {url}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: cdn_loader.py <cdn_urls.txt>", file=sys.stderr)
        sys.exit(1)

    manifest = Path(sys.argv[1])
    count = 0
    for pair in load_from_manifest(manifest):
        # Example: print or feed to trainer
        print(json.dumps(pair, ensure_ascii=False))
        count += 1

    print(f"[cdn_loader] emitted {count} pairs", file=sys.stderr)
```

Install deps (if not present):
```bash
pip install requests pyarrow
```

---

### 3) Update training launcher to use snapshot + CDN

Example snippet to embed in Lightning training script (or notebook):

```python
from pathlib import Path
import subprocess
import json

# 1) Produce snapshot once (or reuse existing)
repo = "axentx/surrogate-1-training-pairs"
date = "2026-05-03"
snapshot_dir = Path(f"snapshots/{repo.replace('/', '-')}/{date}")

if not (snapshot_dir / "cdn_urls.txt").exists():
    subprocess.run(
        ["bash", "bin/snapshot.sh", repo, date],
        check=True,
    )

# 2) Use CDN loader to build dataset (zero HF API calls during training)
from bin.cdn_loader import load_from_manifest

pairs = list(load_from_manifest(snapshot_dir / "cdn_urls.txt"))
print(f"Loaded {len(pairs)} pairs via CDN")

# 3) Convert to HF Dataset locally (optional) or feed directly to trainer
from datasets import Dataset
ds = Dataset.from_list(pairs)
```

---

### 4) Quick validation checklist

- [ ] `chmod +x bin/snapshot.sh`
- [ ] `pip install requests pyarrow`
- [ ] Run snapshot once: `HF_TOKEN=... ./bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-05-03`
- [ ] Verify outputs: `snapshots/axentx-surrogate-1-training-pairs/2026-05-03/{files.json,cdn_urls.txt}`
- [ ] Test loader: `python bin/cdn_loader.py snapshots/axentx-surrogate-1-training-pairs/2026-05-03/cdn_urls.txt | head`
- [ ] Confirm training script uses CDN URLs and no `load_dataset(streaming=True)` on heterogeneous repo.

---

### 5) Why this is high-value (<2h)

- One small script + one loader = ~60–90 lines total.
- Eliminates HF API rate-limit exposure during training (the key 2026-04-29 insight).
- Reuses existing GitHub Actions workers (they already stream via CDN) — no infra changes.
- Deterministic manifests enable reproducible training runs and faster iteration on Lightning Studio (reuse running studios, avoid quota waste).
