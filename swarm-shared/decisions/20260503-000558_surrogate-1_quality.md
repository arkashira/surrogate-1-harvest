# surrogate-1 / quality

## Implementation Plan (≤2h) — Highest-Value Quality Improvement

**Goal**: Eliminate HF API rate-limit risk during training by switching to CDN-only fetches via a pre-flight snapshot.  
**Scope**: Add `bin/snapshot.sh` + integrate into training so Lightning workers never call `list_repo_files` or `load_dataset(..., streaming=True)` on heterogeneous schemas.

### Why this is highest-value
- Prevents 429s during long training runs (CDN bypasses auth rate limits).
- Avoids `pyarrow.CastError` from mixed-schema repos by downloading individual files and projecting `{prompt, response}` at parse time.
- Enables deterministic shard→repo selection to respect HF commit cap (128/hr/repo) via hash-slug routing.
- Fits in <2h: one new script + small train.py patch + optional cron entry.

---

## 1) Snapshot generator (`bin/snapshot.sh`)

Single API call per date folder → JSON manifest saved to `batches/manifests/`.  
Lightning training loads this JSON and fetches via CDN URLs only.

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... ./bin/snapshot.sh <date> [repo]
# Example: ./bin/snapshot.sh 2026-05-03 axentx/surrogate-1-training-pairs

set -euo pipefail

REPO="${2:-axentx/surrogate-1-training-pairs}"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="batches/manifests/${DATE}"
OUTFILE="${OUTDIR}/files.json"

mkdir -p "${OUTDIR}"

# Single API call: non-recursive per folder to avoid pagination explosion
# If folder has subfolders, extend with loop; for now assume flat date folder.
echo "Listing ${REPO} @ ${DATE}/ ..."
FILES=$(python3 - <<PY
import os, json, sys
from huggingface_hub import list_repo_tree
repo = os.environ.get("REPO", "${REPO}")
date_path = "${DATE}"
tree = list_repo_tree(repo=repo, path=date_path, recursive=False)
# Keep only files (not dirs). Use path relative to repo root.
files = [f.rfilename for f in tree if f.type == "file"]
print(json.dumps(files))
PY
)

echo "${FILES}" > "${OUTFILE}"
echo "Snapshot saved: ${OUTFILE}"
echo "Count: $(echo "${FILES}" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")"
```

Make executable:

```bash
chmod +x bin/snapshot.sh
```

---

## 2) Training loader patch (CDN-only, schema-safe)

Replace `load_dataset(streaming=True)` with:

1. Read `files.json` produced by snapshot.
2. For each file, download via CDN URL (no auth header required for public datasets).
3. Parse with `datasets` or `pyarrow` projecting only `{prompt, response}`.

Minimal loader snippet (`train.py` or `data.py`):

```python
# train.py (excerpt)
import json, os, pyarrow.parquet as pq, requests, io
from pathlib import Path
from typing import List, Dict

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-training-pairs")
DATE = os.getenv("DATE", "2026-05-03")
MANIFEST = Path(f"batches/manifests/{DATE}/files.json")

def cdn_url(file_path: str) -> str:
    return f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main/{file_path}"

def load_pairs_from_snapshot() -> List[Dict[str, str]]:
    if not MANIFEST.exists():
        raise FileNotFoundError(f"Snapshot missing: {MANIFEST}. Run bin/snapshot.sh first.")
    with open(MANIFEST) as f:
        files = json.load(f)

    pairs = []
    for fpath in files:
        if not fpath.endswith(".parquet"):
            continue
        url = cdn_url(fpath)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        table = pq.read_table(io.BytesIO(resp.content))
        # Project only required columns; tolerate schema variance
        has_prompt = "prompt" in table.column_names
        has_response = "response" in table.column_names
        if not (has_prompt and has_response):
            # Best-effort fallback: skip or map alternate names
            continue
        df = table.select(["prompt", "response"]).to_pandas()
        for _, row in df.iterrows():
            pairs.append({"prompt": str(row.prompt), "response": str(row.response)})
    return pairs
```

Notes:
- No `datasets.load_dataset` → no pyarrow schema merge errors.
- CDN URLs bypass HF API auth limits.
- Deterministic file list from snapshot → reproducible training.

---

## 3) Commit-cap mitigation (optional but recommended)

If you write enriched outputs back to HF, spread across siblings:

```python
import hashlib

def pick_sibling_repo(slug: str, n: int = 5) -> str:
    # Deterministic shard -> repo to respect 128/hr/repo cap
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    idx = h % n
    return f"axentx/surrogate-1-enriched-{idx}"
```

Use in enrichment/upload scripts to distribute commits.

---

## 4) Cron / workflow integration

Add a daily pre-flight step (or run before training):

```yaml
# .github/workflows/snapshot.yml (optional)
name: snapshot
on:
  schedule:
    - cron: "0 2 * * *"   # daily 02:00 UTC
  workflow_dispatch:
jobs:
  snapshot:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install huggingface_hub pyarrow
      - run: HF_TOKEN=${{ secrets.HF_TOKEN }} ./bin/snapshot.sh 2026-05-03
      - uses: actions/upload-artifact@v4
        with:
          name: manifest
          path: batches/manifests/
```

Then in Lightning training job, fetch artifact or regenerate snapshot once per run (single API call) and proceed with CDN-only fetches.

---

## 5) Validation checklist (quick)

- [ ] `chmod +x bin/snapshot.sh`
- [ ] `HF_TOKEN=... ./bin/snapshot.sh 2026-05-03` produces `batches/manifests/2026-05-03/files.json`
- [ ] `train.py` loads manifest and downloads via CDN (no `load_dataset` on full repo)
- [ ] Training runs without 429s and without `pyarrow.CastError`
- [ ] If uploading, use sibling repo hashing to respect commit cap

ETA: ~90 minutes (script + loader + smoke test).
