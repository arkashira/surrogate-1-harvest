# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**  
Eliminate recursive and per-file authenticated HF API calls. Replace with **one non-recursive `list_repo_tree` per date folder + deterministic sharding + CDN-only downloads**. This removes 429 rate-limit risk, reduces per-shard API calls from O(files) → O(1), and guarantees zero authenticated calls during training.

---

### Concrete steps (actionable)

1. **Add file-list builder** (`tools/build_filelist.py`)  
   - Single `list_repo_tree(repo, path=DATE, recursive=False)` call.  
   - Deterministic, sorted JSON output: `{"date":"...","repo":"...","files":[...]}`.  
   - Exit non-zero on API failure so CI fails fast.

2. **Update `bin/dataset-enrich.sh`**  
   - Accept required `DATE` and `SHARD_ID` (0–15); optional `REPO` and `TOTAL_SHARDS` (default 16).  
   - Accept precomputed `file-list.json` (by path or stdin) to guarantee all shards use the same snapshot.  
   - Deterministic shard assignment: `hash(slug) % TOTAL_SHARDS == SHARD_ID` (use Python `hashlib.md5` for cross-platform stability).  
   - Download via **CDN URLs only**:  
     `https://huggingface.co/datasets/REPO/resolve/main/DATE/{file}`  
     No Authorization header; use `curl --retry 3 --retry-delay 5 -fsSL`.  
   - Keep idempotent behavior: tolerate duplicate uploads across runs.

3. **Update GitHub Actions matrix**  
   - Pass `DATE` and the same `file-list.json` (artifact or inline) to every shard.  
   - Keep 16-shard matrix; each shard writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

4. **Update training guidance (README)**  
   - Embed the same file list in `train.py`.  
   - Use CDN-only fetches during Lightning training to guarantee zero API calls while streaming.

5. **Keep dedup unchanged but tolerant**  
   - Central md5 store on HF Space remains; workers remain idempotent for duplicate uploads.

---

### Code snippets

#### `tools/build_filelist.py`
```python
#!/usr/bin/env python3
"""
Single non-recursive HF API call to list files for a date folder.
Usage:
  python tools/build_filelist.py --repo axentx/surrogate-1-training-pairs --date 2026-04-29 > file-list.json
"""
import argparse
import json
import sys

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser(description="List repo tree non-recursively for a date folder.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    parser.add_argument("--out", default=None, help="Optional output file (default: stdout)")
    args = parser.parse_args()

    api = HfApi()
    try:
        entries = api.list_repo_tree(
            repo_id=args.repo,
            path=args.date,
            recursive=False,
            repo_type="dataset",
        )
    except Exception as exc:
        print(f"ERROR: failed to list repo tree: {exc}", file=sys.stderr)
        sys.exit(1)

    files = sorted([e.path for e in entries if e.type == "file"])
    payload = {"date": args.date, "repo": args.repo, "files": files}

    out_f = open(args.out, "w") if args.out else sys.stdout
    try:
        json.dump(payload, out_f, indent=2)
        out_f.write("\n")
    finally:
        if args.out:
            out_f.close()

if __name__ == "__main__":
    main()
```

#### `bin/dataset-enrich.sh` (core worker logic)
```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Uses CDN-only downloads and deterministic shard assignment.
set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:?DATE is required (e.g. 2026-04-29)}"
SHARD_ID="${SHARD_ID:?SHARD_ID is required (0-15)}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
WORKDIR=$(mktemp -d)
cd "$WORKDIR"

# Accept FILE_LIST as JSON file or read newline-separated file list from stdin.
if [[ -n "${FILE_LIST:-}" && -f "$FILE_LIST" ]]; then
  mapfile -t ALL_FILES < <(jq -r '.files[]' "$FILE_LIST")
else
  mapfile -t ALL_FILES
fi

if [[ ${#ALL_FILES[@]} -eq 0 ]]; then
  echo "No files to process."
  exit 0
fi

# Deterministic shard assignment by slug (filename without extension)
shard_files=()
for f in "${ALL_FILES[@]}"; do
  slug=$(basename "$f" | sed 's/\.[^.]*$//')
  hash_val=$(python3 -c "import hashlib; print(int(hashlib.md5('$slug'.encode()).hexdigest(), 16))")
  if (( hash_val % TOTAL_SHARDS == SHARD_ID )); then
    shard_files+=("$f")
  fi
done

echo "Shard $SHARD_ID processing ${#shard_files[@]} files (total ${#ALL_FILES[@]})"

OUTDIR="output"
mkdir -p "$OUTDIR"
TIMESTAMP=$(date -u +"%H%M%S")
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

# Central dedup helper (assumes lib/dedup.py exposes should_keep(hash) -> bool)
DEDUP_PY="$(cd "$(dirname "$0")" && pwd)/lib/dedup.py"

process_file() {
  local relpath="$1"
  local url="https://huggingface.co/datasets/${REPO}/resolve/main/${relpath}"
  local tmpf
  tmpf=$(mktemp)
  # CDN download — no auth header, bypasses API rate limits
  if ! curl -fsSL --retry 3 --retry-delay 5 -o "$tmpf" "$url"; then
    echo "WARN: failed to download $url"
    rm -f "$tmpf"
    return 1
  fi

  # Project to {prompt,response} and compute md5 hash for dedup.
  python3 - "$tmpf" "$relpath" <<'PYEOF'
import sys, json, hashlib

tmpf, relpath = sys.argv[1], sys.argv[2]

def hash_content(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True).encode()).hexdigest()

def extract_pair(obj):
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
    response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
    return {"prompt": prompt, "response": response}

try:
    if tmpf.endswith(".parquet"):
        import pyarrow.parquet as pq
        tbl = pq.read_table(tmpf)
        df = tbl.to_pandas()
        for _, row in df.iterrows():
            pair = extract_pair(row.to_dict())
            h = hash_content(pair)
            print(json.dumps({"hash": h, "pair": pair, "source": relpath}))
    else:
        with open(tmpf, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                pair = extract_pair(obj)
                h = hash_content(pair)
                print(json.dumps({"hash": h, "pair": pair, "source": relpath}))
except Exception as e:
    print(f"ERROR processing {relpath}: {e}", file=sys.stderr)
PYEOF

  # Dedup check (idempotent)
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    h=$(echo "$line" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['hash
