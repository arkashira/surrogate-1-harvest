# surrogate-1 / quality

## Final Synthesis: CDN-Bypass Ingestion Pipeline (Deterministic, Schema-Safe, 16-Shard)

**Core decision:** Replace `dataset-enrich.sh` with a manifest-driven, CDN-only ingestion script that removes HF API rate limits and mixed-schema errors while enabling reliable 16-way parallelism.

---

## 1. One-Time Setup (Mac orchestrator) — 10 minutes
Generate a deterministic file manifest for today and commit it so shards can fetch via CDN (no auth, no 429).

```bash
#!/usr/bin/env bash
# bin/gen-manifest.sh
set -euo pipefail
REPO="axentx/surrogate-1-training-pairs"
DATE=$(date -u +%Y%m%d)
OUT="manifests/${DATE}.json"
mkdir -p manifests

python3 - <<PY
import os, json, datetime
from huggingface_hub import list_repo_tree

repo = os.environ["REPO"]
date = datetime.datetime.utcnow().strftime("%Y%m%d")
tree = list_repo_tree(repo, path=date, recursive=False)
files = sorted(f.rfilename for f in tree if f.rfilename.endswith(('.jsonl', '.parquet')))
print(json.dumps({"date": date, "files": files}, separators=(",", ":")))
PY > "$OUT"

git add manifests/
git commit -m "manifest: ${DATE}"
git push
```

Run once per day (or via cron) after the daily folder is populated.

---

## 2. New Worker Script — 45 minutes
Deterministic shard routing, schema projection, CDN-only downloads, and central dedup.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich-cdn.sh
# Replaces dataset-enrich.sh
# Usage: SHARD_ID=0 MANIFEST_URL=<url> bash bin/dataset-enrich-cdn.sh
set -euo pipefail
export SHELL=/bin/bash

SHARD_ID="${SHARD_ID:?required}"
MANIFEST_URL="${MANIFEST_URL:?required}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
OUT_DIR="batches/public-merged/$(date -u +%Y%m%d)"
mkdir -p "$OUT_DIR"
TS=$(date -u +%Y%m%d%H%M%S)
OUTPUT="${OUT_DIR}/shard-${SHARD_ID}-${TS}.jsonl"

python3 - <<PY
import json, hashlib, os, sys, tempfile, urllib.request
from pathlib import Path

try:
    import pyarrow.parquet as pq
except Exception:
    pq = None

SHARD_ID = int(os.environ["SHARD_ID"])
TOTAL_SHARDS = int(os.environ.get("TOTAL_SHARDS", "16"))
MANIFEST_URL = os.environ["MANIFEST_URL"]
OUTPUT = os.environ["OUTPUT"]

def shard_for(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % TOTAL_SHARDS

def project_to_pair(local_path: str):
    suffix = Path(local_path).suffix.lower()
    if suffix == ".parquet" and pq is not None:
        tbl = pq.read_table(local_path, columns=["prompt", "response"])
        df = tbl.to_pandas()
        for _, row in df.iterrows():
            yield {"prompt": str(row["prompt"]), "response": str(row["response"])}
    else:
        with open(local_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                yield {"prompt": str(obj["prompt"]), "response": str(obj["response"])}

# Central dedup store (repo-relative)
DEDUP_DB = Path("lib/dedup_db.jsonl")
DEDUP_DB.parent.mkdir(parents=True, exist_ok=True)

def is_duplicate(text: str) -> bool:
    digest = hashlib.md5(text.encode()).hexdigest()
    # Fast check: if db exists, scan digests
    if DEDUP_DB.exists():
        with open(DEDUP_DB) as f:
            for line in f:
                line = line.strip()
                if line == digest:
                    return True
    return False

def mark_seen(text: str):
    digest = hashlib.md5(text.encode()).hexdigest()
    with open(DEDUP_DB, "a", encoding="utf-8") as f:
        f.write(digest + "\n")

# Download manifest via CDN
with urllib.request.urlopen(MANIFEST_URL) as resp:
    manifest = json.loads(resp.read())

out_f = open(OUTPUT, "w", encoding="utf-8")
processed = 0
skipped_dup = 0

for f in manifest["files"]:
    slug = Path(f).stem
    if shard_for(slug) != SHARD_ID:
        continue

    url = f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{f}"
    with tempfile.NamedTemporaryFile(suffix=Path(f).suffix, delete=False) as tmp:
        tmp_path = tmp.name
        try:
            urllib.request.urlretrieve(url, tmp_path)
            for pair in project_to_pair(tmp_path):
                prompt = pair["prompt"].strip()
                response = pair["response"].strip()
                if not prompt or not response:
                    continue
                text = prompt + "\n" + response
                if is_duplicate(text):
                    skipped_dup += 1
                    continue
                mark_seen(text)
                out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                processed += 1
        finally:
            os.unlink(tmp_path)

out_f.close()
print(f"Shard {SHARD_ID}: wrote {processed} pairs to {OUTPUT} (skipped {skipped_dup} duplicates)")
PY
```

Make executable:
```bash
chmod +x bin/dataset-enrich-cdn.sh
```

---

## 3. GitHub Actions Update — 10 minutes
Pass manifest URL and shard ID; keep 16-shard matrix; ensure dedup DB is available and persisted.

```yaml
# .github/workflows/ingest.yml
name: Ingest (CDN-bypass)

on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:
    inputs:
      manifest_date:
        description: 'Manifest date (YYYYMMDD)'
        required: false

jobs:
  ingest:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4

      - name: Set manifest URL
        run: |
          DATE="${{ github.event.inputs.manifest_date }}"
          DATE="${DATE:-$(date -u +%Y%m%d)}"
          echo "MANIFEST_URL=https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/manifests/${DATE}.json" >> $GITHUB_ENV

      - name: Run shard
        env:
          SHARD_ID: ${{ matrix.shard_id }}
          TOTAL_SHARDS: 16
          MANIFEST_URL: ${{ env.MANIFEST_URL }}
          OUTPUT: batches/public-merged/$(date -u +%Y%m%d)/shard-${{ matrix.shard_id }}-$(date -u +%Y%m%d%H%M%S).jsonl
        run: |
          bash bin/dataset-enrich-cdn.sh

      - name: Upload shard result
        uses: actions/upload-artifact@v4
        with:
          name: shard-${{ matrix.shard_id }}
          path: batches/public-merged/$(date -u +%Y%m%d)/
```

---

## 4. Validation & Rollout — 15 minutes
1. **Dry-run one shard locally**:
   ```bash
   SHARD_ID=0 MANIFEST_URL=https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/manifests/$(date -u +%Y%m%d).json bash bin/dataset-enrich-cdn.sh
   ```
2. **Check output schema**:
   ```bash
   head -n1 batches/public-merged/$(date -u +%Y%m%d
