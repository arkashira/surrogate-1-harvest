# surrogate-1 / backend

**Final Consolidated Implementation Plan**  
*(Strongest parts from Candidates 1 + 2, with contradictions resolved for correctness + concrete actionability)*

---

## 1. Goal
Replace `dataset-enrich.sh` with a **CDN-bypass ingestion pipeline** that:
- Eliminates HF API rate limits (429)
- Avoids `pyarrow` mixed-schema `CastError`
- Enables reliable 16-way parallel ingestion
- Produces clean `{prompt, response}` JSONL shards

---

## 2. Architecture (single source of truth)
- **Mac (or cron)**: one-time `list_repo_tree` → `manifest.json` → commit to repo or inject via `workflow_dispatch`.
- **GitHub Actions matrix (16 shards)**:
  - Each worker reads `manifest.json`.
  - Downloads assigned files via **CDN URLs** (`resolve/main/...`) — no Authorization header, bypasses `/api/` rate limits.
  - Projects `{prompt, response}` from heterogeneous schemas.
  - Dedups via **per-shard SQLite** (fast, no network).
  - Writes shard output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- **HF Space**: remains source-of-truth dedup store (final merge step unchanged).

---

## 3. File Changes (concrete, minimal)

### `bin/manifest-generator.py` (new)
```python
#!/usr/bin/env python3
"""
Generate file manifest for surrogate-1-training-pairs.
Run from Mac (or cron) after rate-limit window clears.
"""
import json, os, sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

REPO = "axentx/surrogate-1-training-pairs"
OUT = "manifest.json"

def main():
    api = HfApi(token=os.getenv("HF_TOKEN"))
    # Top-level folders only (cheap, non-recursive)
    tree = api.list_repo_tree(REPO, recursive=False)
    folders = [entry.path for entry in tree if entry.type == "directory"]

    files = []
    for folder in folders:
        # One call per folder, recursive=True to get all files
        entries = api.list_repo_tree(REPO, path=folder, recursive=True)
        for e in entries:
            if e.type == "file" and e.path.endswith((".parquet", ".jsonl", ".json")):
                files.append({
                    "path": e.path,
                    "cdn_url": f"https://huggingface.co/datasets/{REPO}/resolve/main/{e.path}",
                    "size": e.size or 0
                })

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": REPO,
        "total_files": len(files),
        "files": files
    }

    with open(OUT, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {OUT}")

if __name__ == "__main__":
    main()
```

---

### `bin/dataset-enrich.sh` (replacement)
```bash
#!/usr/bin/env bash
# Surrogate-1 CDN-bypass ingestion worker
# Runs in GitHub Actions matrix (16 shards)
set -euo pipefail

# -- config --
MANIFEST="${MANIFEST_PATH:-manifest.json}"
SHARD_ID="${SHARD_ID:-0}"
SHARD_TOTAL="${SHARD_TOTAL:-16}"
OUT_DIR="output"
DATE=$(date -u +%Y-%m-%d)
TS=$(date -u +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"
HF_TOKEN="${HF_TOKEN:-}"
DATASET_REPO="axentx/surrogate-1-training-pairs"
DEDUP_DB="/tmp/dedup-shard${SHARD_ID}.db"

mkdir -p "$OUT_DIR"

# -- dedup init --
python3 - <<'PY'
import sqlite3
db = sqlite3.connect("$DEDUP_DB")
db.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")
db.commit()
db.close()
PY

# -- worker logic --
python3 - <<'PY'
import json, hashlib, sqlite3, requests, sys, os, io
from typing import Dict, Any

MANIFEST = "$MANIFEST"
SHARD_ID = int("$SHARD_ID")
SHARD_TOTAL = int("$SHARD_TOTAL")
OUT_FILE = "$OUT_FILE"
DEDUP_DB = "$DEDUP_DB"

def project_record(raw: Dict[str, Any]) -> Dict[str, str]:
    """Extract {prompt, response} from heterogeneous schemas."""
    prompt_keys = ("prompt", "instruction", "input", "question", "text")
    response_keys = ("response", "output", "answer", "completion", "text")

    prompt = None
    response = None

    for k in prompt_keys:
        if k in raw and raw[k] and isinstance(raw[k], str):
            prompt = raw[k].strip()
            break
    for k in response_keys:
        if k in raw and raw[k] and isinstance(raw[k], str):
            if k not in prompt_keys or prompt is None:
                response = raw[k].strip()
                break

    if prompt is None or response is None:
        for k, v in raw.items():
            if isinstance(v, str) and len(v) > 20:
                parts = [p.strip() for p in v.split("\n\n") if p.strip()]
                if len(parts) >= 2:
                    if prompt is None:
                        prompt = parts[0]
                    if response is None:
                        response = parts[-1]
                    break

    return {"prompt": prompt or "", "response": response or ""}

def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def main():
    with open(MANIFEST) as f:
        manifest = json.load(f)

    files = manifest["files"]
    my_files = [f for i, f in enumerate(files) if i % SHARD_TOTAL == SHARD_ID]

    db = sqlite3.connect(DEDUP_DB)
    out_f = open(OUT_FILE, "w", buffering=1)
    total = 0
    written = 0
    skipped_dup = 0
    schema_errors = 0

    for info in my_files:
        url = info["cdn_url"]
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()

            if url.endswith(".jsonl"):
                lines = resp.text.strip().splitlines()
                records = [json.loads(l) for l in lines if l.strip()]
            elif url.endswith(".json"):
                records = resp.json()
                if not isinstance(records, list):
                    records = [records]
            elif url.endswith(".parquet"):
                import pyarrow.parquet as pq
                table = pq.read_table(io.BytesIO(resp.content))
                records = table.to_pylist()
            else:
                continue

            for rec in records:
                total += 1
                proj = project_record(rec)
                if not proj["prompt"] or not proj["response"]:
                    schema_errors += 1
                    continue

                key = md5(proj["prompt"] + "\n\n" + proj["response"])
                cur = db.execute("SELECT 1 FROM seen WHERE md5 = ?", (key,))
                if cur.fetchone():
                    skipped_dup += 1
                    continue

                db.execute("INSERT INTO seen (md5) VALUES (?)", (key,))
                out_f.write(json.dumps(proj, ensure_ascii=False) + "\n")
                written += 1

        except Exception as e:
            print(f"Error processing {url}: {e}", file=sys.stderr)

    db.commit()
    db.close()
    out_f.close()

    print(f"Shard {SHARD_ID}: total={total}, written={written}, dup={skipped_dup}, schema_errors={schema_errors}")

if __name__ == "__main__":
    main()
PY
```

---

### `.github/workflows/ingest.yml` (updated)
```yml
name: Ingest

on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:
    inputs:
      manifest_path:
        description: 'Path to manifest.json (relative to repo
