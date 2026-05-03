# surrogate-1 / quality

## Final Implementation Plan — Manifest-Driven CDN-Bypass Ingestion (≤2h)

**Scope**: Replace `bin/dataset-enrich.sh` with a manifest-driven worker that:
- Eliminates HF API rate limits (429) during data fetch by using CDN URLs (`resolve/main/...`)
- Avoids mixed-schema `pyarrow` errors by per-file selective parsing (project to `{prompt,response}` only)
- Uses a pre-listed manifest (JSON) to enable zero-API data loading during training
- Keeps shard isolation and deterministic hashing for dedup compatibility

**Why this is the highest-value 2h fix**
- Removes the 429/1000-5min HF API bottleneck that blocks ingestion
- Fixes `pyarrow.CastError` from heterogeneous repo files (known pattern)
- Enables Lightning Studio training to run CDN-only (no API calls during dataload)
- Reuses existing shard logic (16 runners) and dedup store — minimal change surface

---

### Steps (concrete, ordered)

1. **Create `bin/list-manifest.sh`**  
   - Run once (or per cron) from the Mac/orchestrator after rate-limit window clears.
   - Uses `huggingface_hub.list_repo_tree(recursive=False)` per date folder.
   - Emits `manifests/<date>/file-list.json` containing `{ "repo": "...", "paths": [...] }`.
   - Embeds this list into the runner via `cp manifests/<date>/file-list.json /workspace/file-list.json` before matrix starts (or pass via env).

2. **Rewrite `bin/dataset-enrich.sh` → manifest + CDN fetch**  
   - Accept `FILE_LIST` (path to JSON) and `SHARD_ID`/`N_SHARDS`.
   - Deterministic shard assignment: `hash(slug) % N_SHARDS == SHARD_ID`.
   - For each assigned file:
     - Download via `curl -L "https://huggingface.co/datasets/$REPO/resolve/main/$PATH"`.
     - Parse with per-extension handler (jsonl/parquet/csv/json) and project to `{prompt,response}` only.
     - Compute md5 for dedup (reuse `lib/dedup.py` interface).
     - Stream output to `shard-<SHARD_ID>-<TS>.jsonl` in `batches/public-merged/<date>/`.

3. **Add per-format parser module (`bin/parser.py`)**  
   - Lightweight: detect extension, load minimal data, extract fields by heuristic or config.
   - Avoid `load_dataset(streaming=True)` entirely.
   - Return list of `{prompt, response, _source_file, _row_idx}` dicts.

4. **Update workflow (`ingest.yml`) to generate/pass manifest**  
   - Add a pre-job step that runs `list-manifest.sh` and uploads artifact.
   - Pass artifact to each matrix job; or bake list into repo at run start.
   - Keep 16-shard matrix unchanged.

5. **Smoke test locally**  
   - Run one shard against a small date folder.
   - Verify CDN downloads, parsing, dedup, and output format.

---

### Code snippets

#### `bin/list-manifest.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
OUTDIR="${1:-manifests}"
DATE="${2:-$(date +%Y-%m-%d)}"

mkdir -p "$OUTDIR/$DATE"

python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi

repo = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
date = os.getenv("DATE", "$DATE")
out = f"$OUTDIR/$DATE/file-list.json"

api = HfApi()
tree = api.list_repo_tree(repo=repo, path=date, recursive=False)
files = [f.rfilename for f in tree if f.type == "file"]

result = {"repo": repo, "date": date, "paths": files}
with open(out, "w") as f:
    json.dump(result, f, indent=2)
print(f"Wrote {len(files)} files to {out}")
PY
```

#### `bin/parser.py`
```python
#!/usr/bin/env python3
import json, pyarrow.parquet as pq, pyarrow.csv as pcsv, sys
from pathlib import Path

def parse_file(path: str):
    p = Path(path)
    ext = p.suffix.lower()
    rows = []

    try:
        if ext == ".jsonl":
            with open(p, "r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    obj = json.loads(line)
                    rows.append({
                        "prompt": obj.get("prompt") or obj.get("input") or "",
                        "response": obj.get("response") or obj.get("output") or obj.get("completion") or "",
                        "_source_file": str(p),
                        "_row_idx": i,
                    })
        elif ext == ".parquet":
            tbl = pq.read_table(p, columns=["prompt", "response"])
            for i in range(tbl.num_rows):
                rows.append({
                    "prompt": str(tbl["prompt"][i].as_py()),
                    "response": str(tbl["response"][i].as_py()),
                    "_source_file": str(p),
                    "_row_idx": i,
                })
        elif ext == ".json":
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for i, obj in enumerate(data):
                    rows.append({
                        "prompt": obj.get("prompt") or obj.get("input") or "",
                        "response": obj.get("response") or obj.get("output") or "",
                        "_source_file": str(p),
                        "_row_idx": i,
                    })
            else:
                rows.append({
                    "prompt": data.get("prompt") or data.get("input") or "",
                    "response": data.get("response") or data.get("output") or "",
                    "_source_file": str(p),
                    "_row_idx": 0,
                })
        elif ext == ".csv":
            tbl = pcsv.read_csv(p)
            cols = tbl.column_names
            prompt_col = next((c for c in ["prompt", "input", "question"] if c in cols), cols[0] if cols else "")
            response_col = next((c for c in ["response", "output", "answer"] if c in cols), cols[1] if len(cols) > 1 else "")
            for i in range(tbl.num_rows):
                rows.append({
                    "prompt": str(tbl[prompt_col][i].as_py()) if prompt_col else "",
                    "response": str(tbl[response_col][i].as_py()) if response_col else "",
                    "_source_file": str(p),
                    "_row_idx": i,
                })
        else:
            # fallback: try to read as text lines
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f):
                    line = line.strip()
                    if line:
                        rows.append({"prompt": line, "response": "", "_source_file": str(p), "_row_idx": i})
    except Exception as e:
        print(f"Parser warning: {p} -> {e}", file=sys.stderr)

    return rows
```

#### Updated `bin/dataset-enrich.sh` (core loop)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
N_SHARDS="${N_SHARDS:-16}"
SHARD_ID="${SHARD_ID:-0}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
FILE_LIST="${FILE_LIST:-file-list.json}"
OUT_DIR="batches/public-merged/${DATE}"
TS=$(date +%H%M%S)
OUT_FILE="${OUT_DIR}/shard-${SHARD_ID}-${TS}.jsonl"

mkdir -p "$OUT_DIR"

if [[ ! -f "$FILE_LIST" ]]; then
  echo "FILE_LIST not found: $FILE_LIST" >&2
  exit 1
fi

# Load manifest and assign shards
python3 - "$FILE_LIST" "$N_SHARDS" "$SHARD_ID" "$OUT_FILE" <<'PY'
import json, hashlib, subprocess, os, sys

file_list_path = sys.argv[
