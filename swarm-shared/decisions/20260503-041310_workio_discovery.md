# workio / discovery

## Final Synthesis & Action Plan

**Core insight**: combine **discovery/hardening for ingestion & training** (Candidate 1) with **idempotency + temporal guards for clock events** (Candidate 2). Resolve contradictions by enforcing **deterministic, tenant-aware, idempotent-by-default** behavior at every layer (API, DB, ingestion, cron).

---

### 1. Unified Diagnosis (merged)

- **Discovery/ingestion**: no reproducible discovery surface; missing CDN bypass, tenant routing, and ingestion hardening.
- **Clock/events**: no idempotency or temporal locks; duplicate clock-ins/out possible via LINE retries or UI double-taps.
- **Shared gaps**: no correlation IDs, no replay detection, no runbooks for quota/idle-stop recovery.

---

### 2. Proposed Change (merged + hardened)

Add two cohesive modules:

1. **Deterministic Clock/Event Guard**
   - API: require `Idempotency-Key` header; reject missing keys on POST `/clock`.
   - DB: unique constraint on `(employee_id, date, type)` with `X-Request-ID`/`deliveryId` stored; lock window (e.g., 60s) enforced via row-level check.
   - LINE webhook: store `deliveryId` + `timestamp`; dedupe by `deliveryId` before processing.
   - UI: optimistic update + client-side debounce (3s) to suppress retries.

2. **Discovery & Ingestion Hardening**
   - `workio/scripts/discovery/`
     - `knowledge-rag-query.sh` (market + top-hub query).
     - `hf-cdn-filelist.sh` (single-folder list → `filelist.json`).
     - `surrogate-ingest-guard.py` (reject `streaming=True` on mixed repos; project `prompt/response`; tenant-aware routing; tenant-scoped subdirs to avoid HF commit cap).
   - `workio/scripts/wrappers/`
     - `opus-pr-reviewer.sh`, `active-learning-wrapper.sh` (Bash hardened: `set -euo pipefail`, `SHELL=/bin/bash`, executable bit).
   - `workio/scripts/training/`
     - `lightning-reuse.py` (list + reuse running Lightning Studio; idle-stop guard; quota-safe fallback to L40S).

---

### 3. Implementation (concrete, ready to run)

Run from `/opt/axentx/workio`:

```bash
mkdir -p scripts/discovery scripts/wrappers scripts/training
```

#### `scripts/discovery/knowledge-rag-query.sh`

```bash
#!/usr/bin/env bash
# Usage: ./knowledge-rag-query.sh [--market-analysis] [--top-hub MOC]
# Tags: #business-research #knowledge-rag #graph
set -euo pipefail

RUN_MARKET=false
TOP_HUB="MOC"

for arg in "$@"; do
  case "$arg" in
    --market-analysis) RUN_MARKET=true ;;
    --top-hub) shift; TOP_HUB="${1:-MOC}"; shift ;;
  esac
done

if $RUN_MARKET; then
  echo "== Running market analysis =="
  if command -v granite-business-research.sh >/dev/null 2>&1; then
    granite-business-research.sh
  else
    echo "WARN: granite-business-research.sh not found; skipping."
  fi
fi

echo "== Querying top hub: $TOP_HUB =="
if command -v knowledge-rag >/dev/null 2>&1; then
  knowledge-rag query --hub "$TOP_HUB" --context --limit 10
else
  echo "WARN: knowledge-rag CLI not found; skipping."
fi
```

```bash
chmod +x scripts/discovery/knowledge-rag-query.sh
```

#### `scripts/discovery/hf-cdn-filelist.sh`

```bash
#!/usr/bin/env bash
# Usage: ./hf-cdn-filelist.sh <repo> <date_folder> > filelist.json
# Tags: #huggingface #cdn #rate-limit-bypass
set -euo pipefail

REPO="${1:?missing repo}"
DATE_FOLDER="${2:?missing date_folder}"

python3 - "$REPO" "$DATE_FOLDER" <<'PY'
import json, sys
from huggingface_hub import list_repo_tree

repo = sys.argv[1]
folder = sys.argv[2]

try:
    tree = list_repo_tree(repo=repo, path=folder, recursive=False)
    files = [{"path": f.path, "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{f.path}"} for f in tree if f.type != "directory"]
except Exception:
    import subprocess
    out = subprocess.check_output(["curl", "-s", f"https://huggingface.co/api/datasets/{repo}/tree/{folder}?recursive=false"])
    tree = json.loads(out)
    files = [{"path": f["path"], "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{f['path']}"} for f in tree if f.get("type") != "tree"]

sys.stdout.write(json.dumps({"repo": repo, "folder": folder, "files": files}, indent=2))
PY
```

```bash
chmod +x scripts/discovery/hf-cdn-filelist.sh
```

#### `scripts/discovery/surrogate-ingest-guard.py`

```python
#!/usr/bin/env python3
"""
Surrogate-1 ingestion guard.
- Rejects streaming=True on heterogeneous repos.
- Projects to {prompt, response} only.
- Writes to batches/mirror-merged/{date}/{tenant_slug}/{slug}.parquet
- Tenant-aware routing to avoid HF commit cap (128/hr/repo).
Tags: #training #pyarrow #hf-datasets #schema #surrogate-1
"""
import argparse, hashlib, os, sys
from pathlib import Path

def safe_hash_slug(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]

def tenant_slug_from_path(p: str) -> str:
    # Derive tenant from path or filename; fallback to 'shared'.
    name = Path(p).stem.lower()
    parts = [ch for ch in name.split("_") if ch]
    return parts[0] if parts else "shared"

def main():
    parser = argparse.ArgumentParser(description="Surrogate-1 ingestion guard")
    parser.add_argument("--input-files", nargs="+", required=True, help="Local files")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--tenant", default=None, help="Tenant id (auto-derived if omitted)")
    parser.add_argument("--out-dir", default="batches/mirror-merged", help="Output root")
    args = parser.parse_args()

    rows = []
    for p in args.input_files:
        try:
            import pyarrow.parquet as pq
            tbl = pq.read_table(p, columns=["prompt", "response"])
        except Exception:
            import json, pyarrow as pa
            tbl_rows = []
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    tbl_rows.append({"prompt": obj.get("prompt", ""), "response": obj.get("response", "")})
            tbl = pa.Table.from_pylist(tbl_rows, schema=pa.schema([("prompt", pa.string()), ("response", pa.string())]))

        for batch in tbl.to_batches():
            rows.append(batch)

    if not rows:
        print("No rows to write.", file=sys.stderr)
        sys.exit(1)

    combined = pa.concat_tables([pa.Table.from_batches([r]) for r in rows]).combine_chunks()
    slug = safe_hash_slug(" ".join(str(p) for p in args.input_files))
    tenant = args.tenant or tenant_slug_from_path(args.input_files[0])
    out_dir = Path(args.out_dir) / f"{args.date}" / tenant
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.parquet"
    pq.write_table(combined, out_path)
    print(f"Wrote {len(combined)} rows -> {out_path}")

if __name__ == "__main__":
    main()
```

```bash
chmod +x scripts/discovery/surrogate-ingest-guard.py
```

#### `scripts/wrappers/opus-pr-reviewer.sh`

```bash
#!/usr/bin
