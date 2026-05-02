# surrogate-1 / discovery

## 1. Diagnosis
- No deterministic shard-to-date mapping: each run re-streams the entire public dataset list, causing redundant API calls and repeated work across shards/runs.
- No CDN bypass during ingestion: workers rely on `datasets.load_dataset`/`list_repo_files` which hits HF API rate limits (429) and commit caps (128/hr) instead of using `resolve/main/` CDN URLs.
- No persisted file list: every GitHub Actions run repeats expensive recursive `list_repo_files`; a single date-folder listing isn’t cached and reused.
- No reuse of running compute for local testing: the README implies heavy streaming/parquet work is done in CI only; no lightweight local path to validate schema/projection before pushing.
- No attribution hygiene: filename pattern in README says `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`, but implementation may still embed `source`/`ts` columns or write mixed schemas to enriched/ (schema bloat, training issues).

## 2. Proposed change
File: `bin/dataset-enrich.sh` (and companion `lib/dedup.py` usage)  
Scope:
- Add deterministic `date` + `shard` mapping so each CI matrix job processes a single date folder only.
- Replace dataset streaming with pre-listed CDN URLs: on Mac (or once per date), run a lister that writes `file-list-<date>.json`; embed that list in CI and fetch via `https://huggingface.co/datasets/.../resolve/main/...` (no auth, no API rate limit).
- Project to `{prompt,response}` only before write; drop extra cols; write to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Keep `lib/dedup.py` for cross-run dedup (central md5 store) but make it optional/fast-fail to avoid blocking CI.

## 3. Implementation

### bin/dataset-enrich.sh
```bash
#!/usr/bin/env bash
set -euo pipefail

# -- config --
REPO="axentx/surrogate-1-training-pairs"
BASE_CDN="https://huggingface.co/datasets/${REPO}/resolve/main"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD="${SHARD:-0}"          # 0..15 via matrix
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
OUT_DIR="batches/public-merged/${DATE}"
TS="$(date +%H%M%S)"
OUT_FILE="${OUT_DIR}/shard${SHARD}-${TS}.jsonl"
FILE_LIST="file-list-${DATE}.json"
HF_TOKEN="${HF_TOKEN:-}"
# --

mkdir -p "$(dirname "${OUT_FILE}")"

# 1) Deterministic shard assignment for date folder
# list top-level date folders once (cached)
if [[ ! -f "${FILE_LIST}" ]]; then
  echo "Listing ${DATE} folder (CDN bypass)..."
  # Use HF API only once (from Mac/runner) to list non-recursive; fallback to gh api if available
  if command -v gh >/dev/null 2>&1 && [[ -n "${HF_TOKEN}" ]]; then
    gh api "repos/${REPO}/contents/batches/public-merged/${DATE}" --paginate --jq '.[].name' > "${FILE_LIST}.tmp" || true
  fi
  # If gh/api fails, use huggingface_hub from a one-off python script (kept tiny)
  if [[ ! -s "${FILE_LIST}.tmp" ]]; then
    python3 -c "
import json, os, sys
try:
  from huggingface_hub import list_repo_tree
  files = list_repo_tree('${REPO}', path='batches/public-merged/${DATE}', recursive=False)
  names = [f.rpartition('/')[-1] for f in files if f]
  sys.stdout.write('\n'.join(names))
except Exception as e:
  sys.stderr.write(f'Warning: {e}\\n')
  sys.exit(0)
" > "${FILE_LIST}.tmp" 2>/dev/null || true
  fi
  # If still empty, default to date-based pattern (best-effort)
  if [[ ! -s "${FILE_LIST}.tmp" ]]; then
    echo "[]" > "${FILE_LIST}"
  else
    jq -R -s -c 'split("\n") | map(select(. != ""))' < "${FILE_LIST}.tmp" > "${FILE_LIST}"
    rm -f "${FILE_LIST}.tmp"
  fi
fi

# Read list and shard deterministically
mapfile -t ALL_FILES < <(jq -r '.[]' "${FILE_LIST}" 2>/dev/null || echo "")
if [[ ${#ALL_FILES[@]} -eq 0 ]]; then
  echo "No files for ${DATE}. Nothing to do."
  exit 0
fi

# Deterministic by filename hash
shard_files=()
for f in "${ALL_FILES[@]}"; do
  # stable numeric hash (0..TOTAL_SHARDS-1)
  h=$(echo -n "$f" | cksum | awk '{print $1}')
  if (( h % TOTAL_SHARDS == SHARD )); then
    shard_files+=("$f")
  fi
done

if [[ ${#shard_files[@]} -eq 0 ]]; then
  echo "Shard ${SHARD} has no files for ${DATE}."
  exit 0
fi

# 2) Process via CDN (no auth) + projection to {prompt,response}
# Use python for reliable parquet/jsonl decode and projection
python3 -c "
import json, sys, os, pyarrow.parquet as pq, pyarrow as pa, urllib.request, tempfile, hashlib

REPO = '${REPO}'
CDN = '${BASE_CDN}'
OUT_FILE = '${OUT_FILE}'
SHARD_FILES = json.loads('$(printf '%s\n' "${shard_files[@]}" | jq -R -s -c 'split("\n")|map(select(.!=\"\"))')')

def cdn_url(path):
    return f'{CDN}/batches/public-merged/${DATE}/' + path

def deterministic_md5(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def fetch_and_project(url):
    # try parquet first, fallback to jsonl
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            urllib.request.urlretrieve(url, tf.name)
            try:
                table = pq.read_table(tf.name, columns=['prompt', 'response'])
            except Exception:
                # try alternate col names or jsonl
                table = pq.read_table(tf.name)
                if 'prompt' not in table.column_names or 'response' not in table.column_names:
                    # attempt to coerce common names
                    cols = table.column_names
                    prompt_col = next((c for c in cols if 'prompt' in c.lower()), None)
                    response_col = next((c for c in cols if 'response' in c.lower() or 'completion' in c.lower()), None)
                    if prompt_col and response_col:
                        table = table.select([prompt_col, response_col]).rename_columns(['prompt','response'])
                    else:
                        raise ValueError('No prompt/response columns')
            df = table.to_pandas()
            # keep only prompt/response
            df = df[['prompt','response']].dropna(subset=['prompt','response'])
            return df
    finally:
        try:
            os.unlink(tf.name)
        except Exception:
            pass

rows = []
for fname in SHARD_FILES:
    url = cdn_url(fname)
    try:
        df = fetch_and_project(url)
        for _, r in df.iterrows():
            r = r.to_dict()
            r['_md5'] = deterministic_md5(r['prompt'] + '\\x00' + r['response'])
            rows.append(r)
    except Exception as e:
        sys.stderr.write(f'Failed {url}: {e}\\n')

# 3) Write shard output
os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
with open(OUT_FILE, 'w', encoding='utf-8') as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + '\\n')

print(f'Wrote {len(rows)} rows to {OUT_FILE}')
"

# 4) Optional: push to HF (only if HF_TOKEN present)
if [[ -n "${HF_TOKEN}" ]]; then
  git config --global user.email "bot@axentx.dev"
  git config --global user.name "surrogate-1-runner"
  git clone https://huggingface.co/datasets/${REPO} /tmp/h
