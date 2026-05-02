# surrogate-1 / quality

## Final Synthesis — Deterministic CDN-only ingestion (resilient to HF API 429s)

**Chosen approach**
- Single pre-flight snapshot (`file-list.json`) produced once per workflow run for the target date folder.
- All 16 shards read that snapshot and download exclusively via HF CDN (`resolve/main/...`).  
- Deterministic shard assignment: `shard_id = int(md5(slug) % 16)` where `slug` is basename without extension.  
- No `datasets`/`hf_api` calls during training; retries/backoff only for CDN HTTP errors.  
- Keep existing per-run dedup (central md5 store) and schema projection; output `shard<N>-<HHMMSS>.jsonl`.

---

## Implementation (≤2h)

### 1) Add `tools/list_repo_files.py`
Produces deterministic snapshot for a date folder.

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in axentx/surrogate-1-training-pairs.
Usage:
  HF_TOKEN=<token> python3 tools/list_repo_files.py --date 2026-05-02 --out file-list.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser(description="List repo files for a date folder.")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-05-02)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--repo", default=REPO_ID, help="HF dataset repo id")
    args = parser.parse_args()

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    entries = api.list_repo_tree(
        repo_id=args.repo,
        path=args.date,
        repo_type="dataset",
        recursive=False,
    )

    files = []
    for e in entries:
        if e.type == "file":
            files.append({"path": f"{args.date}/{e.path}", "size": e.size})

    out_path = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(files, f, indent=2)

    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

---

### 2) Update `bin/dataset-enrich.sh`
Accept `FILE_LIST` and switch to CDN-only ingestion with deterministic shard routing.

```bash
#!/usr/bin/env bash
set -euo pipefail

HF_REPO="axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-${GITHUB_MATRIX_SHARD:-0}}"
FILE_LIST="${FILE_LIST:-}"   # e.g. batches/public-merged/2026-05-02/file-list.json
OUT_DIR="batches/public-merged/${DATE}"
TS="$(date +%H%M%S)"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "$OUT_DIR"

python3 -c "
import hashlib, json, os, sys, requests, time, io

HF_REPO = os.environ['HF_REPO']
SHARD_ID = int(os.environ['SHARD_ID'])
OUT_FILE = os.environ['OUT_FILE']
FILE_LIST = os.environ.get('FILE_LIST')

def shard_for(path: str) -> int:
    slug = os.path.splitext(os.path.basename(path))[0]
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % 16

def cdn_url(path: str) -> str:
    return f'https://huggingface.co/datasets/{HF_REPO}/resolve/main/{path}'

def project_to_pair(raw_bytes: bytes, path: str):
    # Minimal schema projection: try parquet then json heuristics.
    try:
        import pyarrow.parquet as pq
        table = pq.read_table(io.BytesIO(raw_bytes), columns=['prompt', 'response'])
        return table.to_pylist()
    except Exception:
        pass
    try:
        data = json.loads(raw_bytes)
        if isinstance(data, list):
            return [item for item in data if 'prompt' in item and 'response' in item]
        if isinstance(data, dict) and 'prompt' in data and 'response' in data:
            return [data]
    except Exception:
        pass
    return []

def stream_cdn_with_retry(url, max_retries=3, backoff=1.0):
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            # stream into memory for projection (files are small per shard design)
            content = b''.join(chunk for chunk in resp.iter_content(chunk_size=8192))
            return content
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == max_retries:
                    raise
                sleep_t = backoff * (2 ** (attempt - 1))
                print(f'WARN: CDN retry {attempt}/{max_retries} for {url} ({resp.status_code}), sleeping {sleep_t}s', file=sys.stderr)
                time.sleep(sleep_t)
                continue
            raise
        except Exception:
            if attempt == max_retries:
                raise
            time.sleep(backoff * (2 ** (attempt - 1)))

dedup = set()
rows = []

if FILE_LIST:
    with open(FILE_LIST) as f:
        entries = json.load(f)
    paths = [e['path'] for e in entries]
else:
    # fallback (not recommended for training): use API once
    try:
        from huggingface_hub import HfApi
        api = HfApi()
        entries = api.list_repo_tree(repo_id=HF_REPO, path=DATE, repo_type='dataset', recursive=False)
        paths = [f'{DATE}/{e.path}' for e in entries if e.type == 'file']
    except Exception as e:
        print(f'ERROR: no FILE_LIST and fallback failed: {e}', file=sys.stderr)
        sys.exit(1)

for path in paths:
    if shard_for(path) != SHARD_ID:
        continue
    url = cdn_url(path)
    try:
        content = stream_cdn_with_retry(url)
        pairs = project_to_pair(content, path)
        for item in pairs:
            pid = item.get('prompt')
            rid = item.get('response')
            if not pid or not rid:
                continue
            h = hashlib.md5(f'{pid}{rid}'.encode()).hexdigest()
            if h in dedup:
                continue
            dedup.add(h)
            rows.append({'prompt': pid, 'response': rid})
    except Exception as e:
        print(f'WARN: failed {path}: {e}', file=sys.stderr)

with open(OUT_FILE, 'w', encoding='utf-8') as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')

print(f'Wrote {len(rows)} pairs to {OUT_FILE}')
"
```

---

### 3) Update GitHub Actions workflow
Generate snapshot once and pass it to all shards.

```yaml
jobs:
  prepare-list:
    runs-on: ubuntu-latest
    outputs:
      file-list: ${{ steps.save-list.outputs.file-list }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install deps
        run: pip install huggingface_hub pyarrow
      - name: Generate file-list.json
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          python3 tools/list_repo_files.py --date "${DATE:-$(date +%Y-%m-%d)}" --out file-list.json
      - name: Upload file-list artifact
        uses: actions/upload-artifact@v4
        with:
          name: file-list
          path:
