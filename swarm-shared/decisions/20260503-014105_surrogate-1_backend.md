# surrogate-1 / backend

## Implementation Plan — CDN-Bypass Manifest-Driven Ingestion

**Scope**: Replace `bin/dataset-enrich.sh` with a manifest-driven, CDN-bypass ingestion worker that:
- Eliminates HF API rate limits (429) during data fetch
- Avoids mixed-schema `pyarrow` errors by per-file selective projection
- Produces clean `{prompt,response}` pairs only
- Keeps shard isolation and deterministic hashing
- Fits <2h implementation window

---

## 2. Concrete Steps (ordered)

1. **Create manifest generator** (`bin/gen-manifest.py`)  
   - Runs on Mac (or cron) once per date folder  
   - Uses single `list_repo_tree(path, recursive=False)` for one date folder  
   - Emits `manifest/<date>/files.json` with `{path, size, sha}`  
   - Deterministic shard assignment: `hash(slug) % 16`

2. **Rewrite worker script** (`bin/dataset-enrich.sh` → `bin/ingest-worker.py`)  
   - Accepts `SHARD_ID` and `MANIFEST_PATH` as env args  
   - Downloads assigned files via CDN URL (`resolve/main/...`) with no auth header  
   - Per-file schema detection → project only `{prompt,response}` keys  
   - Stream-parse JSONL/parquet without full load  
   - Dedup via central `lib/dedup.py` md5 store  
   - Output: `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`

3. **Update workflow** (`.github/workflows/ingest.yml`)  
   - Pass `MANIFEST_PATH` artifact to matrix jobs  
   - Keep 16-shard matrix, but each job runs `python bin/ingest-worker.py`  
   - Add retry/backoff for CDN 429 (CDN limits are higher but possible)

4. **Remove HF dataset streaming** from worker entirely  
   - No `load_dataset(streaming=True)`  
   - No `list_repo_files` recursive calls

---

## 3. Code Snippets

### `bin/gen-manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest for a date folder in surrogate-1-training-pairs.
Usage:
  HF_TOKEN=<token> python bin/gen-manifest.py --repo axentx/surrogate-1-training-pairs --date 2026-05-03
"""
import argparse
import json
import os
import hashlib
from huggingface_hub import HfApi

def slug_from_path(path: str) -> str:
    # Expect: public-raw/2026-05-03/<slug>.jsonl or .parquet
    parts = path.split('/')
    if len(parts) >= 3:
        return parts[-1].split('.')[0]
    return path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', default='axentx/surrogate-1-training-pairs')
    parser.add_argument('--date', required=True, help='Date folder (YYYY-MM-DD)')
    parser.add_argument('--out-dir', default='manifest')
    args = parser.parse_args()

    api = HfApi(token=os.getenv('HF_TOKEN'))
    folder = f'public-raw/{args.date}'
    try:
        tree = api.list_repo_tree(repo_id=args.repo, path=folder, recursive=False)
    except Exception as e:
        print(f"Error listing {folder}: {e}")
        return

    files = []
    for item in tree:
        if item.type != 'file':
            continue
        path = item.path
        slug = slug_from_path(path)
        files.append({
            'path': path,
            'size': getattr(item, 'size', 0),
            'sha': getattr(item, 'sha', ''),
            'slug': slug,
            'shard': hashlib.md5(slug.encode()).hexdigest(),
            'shard_id': int(hashlib.md5(slug.encode()).hexdigest(), 16) % 16,
        })

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, args.date, 'files.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(files, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == '__main__':
    main()
```

### `bin/ingest-worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingest worker for a single shard.
Env:
  SHARD_ID=0..15
  MANIFEST_PATH=manifest/2026-05-03/files.json
  HF_DATASET_REPO=axentx/surrogate-1-training-pairs
  DATE=2026-05-03
  OUT_DIR=batches/public-merged
"""
import os
import sys
import json
import hashlib
import datetime
import pyarrow.parquet as pq
import pyarrow as pa
import requests
from lib.dedup import DedupStore  # central md5 dedup store

HF_REPO = os.getenv('HF_DATASET_REPO', 'axentx/surrogate-1-training-pairs')
CDN_BASE = f'https://huggingface.co/datasets/{HF_REPO}/resolve/main'

def cdn_url(path: str) -> str:
    return f'{CDN_BASE}/{path}'

def project_pair(obj, path: str):
    """Return (prompt, response) or None."""
    # Heuristic schema projection: accept common key names
    prompt_keys = {'prompt', 'Prompt', 'PROMPT', 'input', 'Input', 'INPUT', 'question', 'Question'}
    response_keys = {'response', 'Response', 'RESPONSE', 'output', 'Output', 'OUTPUT', 'answer', 'Answer', 'completion', 'Completion'}

    if isinstance(obj, dict):
        prompt = None
        response = None
        for k, v in obj.items():
            if k in prompt_keys and prompt is None:
                prompt = v
            if k in response_keys and response is None:
                response = v
        if prompt is not None and response is not None:
            return str(prompt), str(response)
    return None

def process_jsonl(url: str, dedup: DedupStore):
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        pair = project_pair(obj, url)
        if pair is None:
            continue
        prompt, response = pair
        md5 = hashlib.md5(f'{prompt}\0{response}'.encode()).hexdigest()
        if dedup.seen(md5):
            continue
        dedup.add(md5)
        yield {'prompt': prompt, 'response': response}

def process_parquet(url: str, dedup: DedupStore):
    # Stream via requests + pyarrow buffer — avoids full download into memory at once
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    try:
        table = pq.read_table(pa.BufferReader(resp.content), columns=['prompt', 'response'])
    except (pa.ArrowInvalid, KeyError):
        # Try fallback: read all and project
        try:
            table = pq.read_table(pa.BufferReader(resp.content))
        except Exception:
            return
        # Selectively project
        cols = [c for c in table.column_names if c.lower() in ('prompt', 'response')]
        if len(cols) < 2:
            return
        table = table.select(cols)
        # Rename to canonical
        rename = {}
        for c in table.column_names:
            if c.lower() == 'prompt':
                rename[c] = 'prompt'
            elif c.lower() == 'response':
                rename[c] = 'response'
        if len(rename) == 2:
            table = table.rename_columns([rename.get(c, c) for c in table.column_names])
        else:
            return

    df = table.to_pandas()
    for _, row in df.iterrows():
        prompt = str(row.get('prompt', ''))
        response = str(row.get('response', ''))
        if not prompt or not response:
            continue
        md5 =
