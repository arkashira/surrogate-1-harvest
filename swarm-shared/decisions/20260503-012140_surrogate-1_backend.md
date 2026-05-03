# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value fix**  
Replace recursive HF API ingestion and per-file authenticated fetches with a single non-recursive `list_repo_tree` + CDN-only fetches. This removes rate-limit pressure, avoids 429s, and keeps ingestion fast and deterministic.

---

### Concrete steps

1. **Add deterministic shard-to-repo routing**  
   Create `lib/repo_router.py`:

   ```python
   import hashlib
   from typing import Tuple

   SIBLING_REPOS = [
       "axentx/surrogate-1-training-pairs",
       "axentx/surrogate-1-training-pairs-sib1",
       "axentx/surrogate-1-training-pairs-sib2",
       "axentx/surrogate-1-training-pairs-sib3",
       "axentx/surrogate-1-training-pairs-sib4",
   ]

   def route_file_to_repo(file_relpath: str) -> str:
       h = int(hashlib.sha256(file_relpath.encode()).hexdigest(), 16)
       return SIBLING_REPOS[h % len(SIBLING_REPOS)]

   def route_shard_to_repo(shard_id: int, shard_total: int) -> str:
       return SIBLING_REPOS[shard_id % len(SIBLING_REPOS)]
   ```

2. **Add file-listing helper**  
   Create `tools/list_files.py` (same as Candidate 1).

3. **Update ingestion script**  
   Replace `bin/dataset-enrich.sh` with:

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   REPO="axentx/surrogate-1-training-pairs"
   DATE="${DATE:-$(date +%Y-%m-%d)}"
   SHARD_TOTAL="${SHARD_TOTAL:-16}"
   SHARD_ID="${SHARD_ID:?required}"
   OUT_DIR="${OUT_DIR:-./out}"
   WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
   cd "$WORKDIR"
   mkdir -p "$OUT_DIR"

   echo "[$(date -u -Iseconds)] Shard $SHARD_ID/$SHARD_TOTAL date=$DATE"

   # 1) List files once (non-recursive)
   python3 tools/list_files.py --repo "$REPO" --date "$DATE" > file-list.json
   mapfile -t ALL_FILES < <(jq -r '.[]' file-list.json)

   # 2) Deterministic shard assignment by slug hash
   shard_files=()
   for f in "${ALL_FILES[@]}"; do
     slug=$(basename "$f" | sed 's/\.[^.]*$//')
     h=$(echo -n "$slug" | cksum | awk '{print $1}')
     if (( h % SHARD_TOTAL == SHARD_ID )); then
       shard_files+=("$f")
     fi
   done
   echo "[$(date -u -Iseconds)] Assigned ${#shard_files[@]} files to this shard"

   # 3) Download via CDN and process
   ts=$(date -u +%H%M%S)
   outfile="${OUT_DIR}/shard-${SHARD_ID}-${ts}.jsonl"
   tmpdir=$(mktemp -d)
   cleanup() { rm -rf "$tmpdir"; }
   trap cleanup EXIT

   count=0
   for rel in "${shard_files[@]}"; do
     url="https://huggingface.co/datasets/${REPO}/resolve/main/${rel}"
     dest="${tmpdir}/$(basename "$rel")"
     for attempt in 1 2 3; do
       if curl -fsSL --retry 2 --retry-delay 1 --retry-max-time 10 -o "$dest" "$url"; then
         break
       fi
       if (( attempt == 3 )); then
         echo "[$(date -u -Iseconds)] ERROR downloading $url" >&2
         continue 2
       fi
       sleep $(( 2 ** attempt ))
     done

     python3 -c "
import sys, json, pyarrow.parquet as pq, hashlib, os
from lib.dedup import is_duplicate, store_hash

def extract_items(path):
    try:
        tbl = pq.read_table(path, columns=['prompt', 'response'])
        for row in tbl.to_pylist():
            yield row.get('prompt'), row.get('response')
        return
    except Exception:
        pass
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                yield obj.get('prompt'), obj.get('response')
        return
    except Exception:
        pass

for prompt, response in extract_items(sys.argv[1]):
    if not prompt or not response:
        continue
    blob = (str(prompt) + str(response)).encode('utf-8')
    md5 = hashlib.md5(blob).hexdigest()
    if is_duplicate(md5):
        continue
    store_hash(md5)
    out = {'prompt': prompt, 'response': response}
    print(json.dumps(out, ensure_ascii=False))
" "$dest" >> "$outfile"
     count=$((count + 1))
   done

   echo "[$(date -u -Iseconds)] Processed $count files -> $outfile"

   # 4) Upload shard file to the correct sibling repo
   python3 -c "
from lib.repo_router import route_shard_to_repo
print(route_shard_to_repo($SHARD_ID, $SHARD_TOTAL))
" > target_repo.txt
   TARGET_REPO=$(cat target_repo.txt)

   hf upload \
     --repo-type dataset \
     --repo-id "$TARGET_REPO" \
     --local-file "$outfile" \
     --path "batches/public-merged/${DATE}/$(basename "$outfile")" \
     --commit-message "Add shard $SHARD_ID for $DATE"
   ```

4. **Update GitHub Actions matrix**  
   Keep 16 shards. Pass `DATE`, `SHARD_ID`, `SHARD_TOTAL` via matrix. Ensure `HF_TOKEN` is available for push only (not for downloads).

5. **Minor hardening**  
   - Retry CDN downloads with exponential backoff (max 3 retries).  
   - Respect `HF_COMMIT_CAP` by using deterministic filenames and avoiding per-file commits.  
   - Reuse existing `lib/dedup.py` for cross-source dedup.
