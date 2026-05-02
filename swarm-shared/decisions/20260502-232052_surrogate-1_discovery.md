# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Goal:** Replace runtime HF API calls + streaming dataset loads in `bin/dataset-enrich.sh` with a deterministic pre-flight snapshot (JSON) and CDN-only fetches. This removes rate-limit exposure, avoids pyarrow schema errors, and keeps shard runners lightweight.

---

## Files to Add/Modify

### 1) Snapshot Generator — `bin/make-snapshot.sh` (NEW)

```bash
#!/usr/bin/env bash
# bin/make-snapshot.sh
# Usage:
#   HF_TOKEN=hf_xxx REPO=axentx/surrogate-1-training-pairs DATE=2026-05-02 ./bin/make-snapshot.sh
#
# Produces:
#   snapshot/<date>/files.json  (deterministic list of file paths)

set -euo pipefail

: "${HF_TOKEN:?required}"
: "${REPO:?required}"
: "${DATE:?required (YYYY-MM-DD)}"

OUTDIR="snapshot/${DATE}"
OUTFILE="${OUTDIR}/files.json"

mkdir -p "${OUTDIR}"

python3 - <<PY > "${OUTFILE}.tmp"
import os, json, sys
from huggingface_hub import HfApi

api = HfApi(token=os.environ["HF_TOKEN"])
repo = os.environ["REPO"]
date = os.environ["DATE"]

entries = api.list_repo_tree(repo=repo, path=date, recursive=False)

files = []
for e in entries:
    # Keep only files (skip subfolders)
    if getattr(e, "type", None) == "file" or (hasattr(e, "path") and "." in e.path):
        files.append(e.path)

files.sort()
print(json.dumps({"date": date, "files": files}, indent=2))
PY

mv "${OUTFILE}.tmp" "${OUTFILE}"
echo "Snapshot written to ${OUTFILE}"
```

```bash
chmod +x bin/make-snapshot.sh
```

---

### 2) Updated Worker — `bin/dataset-enrich.sh` (MODIFIED)

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated to use CDN-only fetches + snapshot.
#
# Environment:
#   SHARD_ID (0-15)
#   HF_TOKEN (for push)
#   SNAPSHOT_FILE (path to JSON produced by make-snapshot.sh)
#   DATE (YYYY-MM-DD) — must match snapshot date
#
# Behavior:
#   - Reads files list from SNAPSHOT_FILE
#   - Deterministically assigns 1/16 slice by slug-hash mod 16
#   - Downloads each assigned file via CDN (no Authorization header)
#   - Projects to {prompt, response}, dedups, and uploads shard output

set -euo pipefail

: "${SHARD_ID:?required (0-15)}"
: "${HF_TOKEN:?required}"
: "${SNAPSHOT_FILE:?required}"
: "${DATE:?required (YYYY-MM-DD)}"

REPO_DST="axentx/surrogate-1-training-pairs"
WORKDIR=$(mktemp -d)
cleanup() { rm -rf "${WORKDIR}"; }
trap cleanup EXIT

# Python helper for projection + dedup
cat > "${WORKDIR}/project_and_dedup.py" <<'PY'
import sys, json, hashlib, pyarrow.parquet as pq, os

def hash_slug(s):
    return int(hashlib.md5(s.encode()).hexdigest(), 16)

def project_file_to_pairs(local_path):
    """Return list of {prompt, response} dicts; skip on error."""
    try:
        if local_path.endswith(".parquet"):
            tbl = pq.read_table(local_path, columns=["prompt", "response"])
            df = tbl.to_pandas()
            return [{"prompt": r.prompt, "response": r.response} for r in df.itertuples()]
        if local_path.endswith(".jsonl"):
            out = []
            with open(local_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    out.append({"prompt": obj["prompt"], "response": obj["response"]})
            return out
    except Exception:
        return []
    return []

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--download-root", required=True)
    args = parser.parse_args()

    seen = set()
    out_rows = []
    with open(args.input, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rel_path = line
            slug = rel_path
            if hash_slug(slug) % 16 != args.shard:
                continue

            local_file = os.path.join(args.download_root, rel_path)
            if not os.path.exists(local_file):
                continue

            pairs = project_file_to_pairs(local_file)
            for p in pairs:
                content = (p.get("prompt", "") + "\x1e" + p.get("response", "")).strip()
                if not content:
                    continue
                h = hashlib.md5(content.encode()).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                out_rows.append({"prompt": p["prompt"], "response": p["response"]})

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        for r in out_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    main()
PY

# Validate snapshot
if [ ! -f "${SNAPSHOT_FILE}" ]; then
  echo "Snapshot file not found: ${SNAPSHOT_FILE}"
  exit 1
fi

mapfile -t ALL_FILES < <(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('\n'.join(d['files']))" "${SNAPSHOT_FILE}")

if [ ${#ALL_FILES[@]} -eq 0 ]; then
  echo "No files in snapshot."
  exit 0
fi

# Download assigned files via CDN (no auth)
DOWNLOAD_ROOT="${WORKDIR}/cdn_downloads"
mkdir -p "${DOWNLOAD_ROOT}"

BASE_CDN="https://huggingface.co/datasets/${REPO_DST}/resolve/main"

download_file() {
  local rel="$1"
  local out="${DOWNLOAD_ROOT}/${rel}"
  mkdir -p "$(dirname "${out}")"
  curl -fsSL --retry 3 --retry-delay 2 --max-time 60 -o "${out}" "${BASE_CDN}/${rel}" || {
    echo "Failed to download ${rel}"
    return 1
  }
}

# Parallel download with concurrency limit
NCPU=$(sysctl -n hw.ncpu 2>/dev/null || echo 4)
JOBS=$((NCPU * 2))

export -f download_file
export DOWNLOAD_ROOT BASE_CDN

printf "%s\n" "${ALL_FILES[@]}" | xargs -P "${JOBS}" -I{} bash -c 'download_file "$@"' _ {}

# Run projection + dedup
python3 "${WORKDIR}/project_and_dedup.py" \
  --input "${SNAPSHOT_FILE}" \
  --output "batches/public-merged/${DATE}/shard${SHARD_ID}-$(date +%H%M%S).jsonl" \
  --shard "${SHARD_ID}" \
  --download-root "${DOWNLOAD_ROOT}"

# Upload to HF (optional, if HF_TOKEN provided)
if [ -n "${HF_TOKEN:-}" ]; then
  OUTPUT_PATH="batches/public-merged/${DATE}/shard${SHARD_ID}-$(date +%H%M%S).jsonl"
  if [ -f "${OUTPUT_PATH}" ]; then
    gh release upload "${DATE}" "${OUTPUT_PATH}" --repo "${REPO_DST}" --clobber || true
  fi
fi
```

```bash
chmod +x bin/dataset-enrich.sh
```

---

## Key Improvements (Resolved Contradictions)

| Conflict | Resolution |
|---|---|
| **Snapshot scope** | Non-recursive `list_repo_tree` per date folder (not full repo) — avoids pagination explosion and keeps snapshot small
