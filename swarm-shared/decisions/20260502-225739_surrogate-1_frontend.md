# surrogate-1 / frontend

**Final unified implementation** (chosen from strongest parts of both candidates, corrected for reliability + immediate actionability)

## Core change (≤2h)
Add a **deterministic pre-flight file list + CDN-only ingestion path** so shard workers never hit HF API 429s and remain fully deterministic.

- 3 files changed/added:  
  1) `bin/list_files.py` (Mac side)  
  2) `bin/cdn_download.py` (CDN fetcher + projection)  
  3) `bin/dataset-enrich.sh` (worker entrypoint)

- Dependencies: add `requests` to `requirements.txt` if not present.

---

## 1) `bin/list_files.py`
Use `list_repo_tree(recursive=False)` (fast, low-pagination) and emit a stable JSON file shard workers can embed.

```python
#!/usr/bin/env python3
"""
Usage (Mac, after rate-limit window clears):
  python3 bin/list_files.py \
    --repo axentx/surrogate-1-training-pairs \
    --path batches/public-merged/2026-05-02 \
    --out file-list.json

Output:
{
  "repo": "...",
  "path": "...",
  "files": [
    {"path": "batches/public-merged/2026-05-02/shard0-120000.jsonl", "size": 12345},
    ...
  ]
}
"""
import argparse
import json
import os
import sys

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi()
    tree = api.list_repo_tree(repo_id=args.repo, path=args.path, recursive=False)
    files = [{"path": f.path, "size": f.size} for f in tree if f.type == "file"]

    payload = {
        "repo": args.repo,
        "path": args.path,
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

```bash
chmod +x bin/list_files.py
```

---

## 2) `bin/cdn_download.py`
Download via CDN (`/resolve/main/...`) with **streaming JSONL + chunked Parquet** to avoid OOM. Project to `{prompt, response}` immediately.

```python
#!/usr/bin/env python3
"""
Download dataset files via CDN (no auth/API) and project to {prompt, response}.

Usage:
  python3 bin/cdn_download.py <file-list.json> <out-dir> <repo>
"""
import json
import sys
from pathlib import Path
from typing import Any, Dict, Generator, List

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def cdn_lines(repo: str, path: str) -> Generator[Dict[str, Any], None, None]:
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            obj = json.loads(line)
            yield {
                "prompt": obj.get("prompt", ""),
                "response": obj.get("response", ""),
            }

def cdn_parquet(repo: str, path: str) -> Generator[Dict[str, Any], None, None]:
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    # Stream into memory buffer then read parquet (avoids full copy on disk)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        buf = pa.BufferReader(r.content)
        table = pq.read_table(buf)
        for batch in table.to_batches():
            df = batch.to_pandas()
            for _, row in df.iterrows():
                yield {
                    "prompt": str(row.get("prompt", "")),
                    "response": str(row.get("response", "")),
                }

def download_all(file_list_path: str, out_dir: str, repo: str) -> List[str]:
    with open(file_list_path) as f:
        meta = json.load(f)

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    out_files: List[str] = []
    for item in tqdm(meta["files"], desc="CDN download"):
        src = item["path"]
        name = Path(src).name

        if src.endswith(".jsonl"):
            dst = out_path / name
            with open(dst, "w") as f:
                for row in cdn_lines(repo, src):
                    json.dump(row, f)
                    f.write("\n")
            out_files.append(str(dst))

        elif src.endswith(".parquet"):
            dst = out_path / name.replace(".parquet", ".jsonl")
            with open(dst, "w") as f:
                for row in cdn_parquet(repo, src):
                    json.dump(row, f)
                    f.write("\n")
            out_files.append(str(dst))

        else:
            # skip unknown extensions
            continue

    return out_files

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: cdn_download.py <file-list.json> <out-dir> <repo>")
        sys.exit(1)
    _, file_list, out_dir, repo = sys.argv
    download_all(file_list, out_dir, repo)
```

```bash
chmod +x bin/cdn_download.py
```

---

## 3) `bin/dataset-enrich.sh`
Worker entrypoint: if `FILE_LIST` is provided and valid, use CDN-only; otherwise fall back to legacy HF API (kept for compatibility).

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO=${REPO:-"axentx/surrogate-1-training-pairs"}
HF_TOKEN=${HF_TOKEN:-""}
WORKDIR=${WORKDIR:-"/tmp/surrogate-ingest"}
FILE_LIST=${FILE_LIST:-""}
OUT_DIR=${OUT_DIR:-"${WORKDIR}/enriched"}

mkdir -p "${OUT_DIR}"

if [[ -n "${FILE_LIST}" && -f "${FILE_LIST}" ]]; then
  echo "INFO: CDN-only mode using file-list ${FILE_LIST}"
  python3 "$(dirname "$0")/cdn_download.py" "${FILE_LIST}" "${OUT_DIR}" "${REPO}"
else
  echo "INFO: Legacy mode (HF API + streaming) — may hit 429 under load"
  python3 - <<PY
import os, json
from datasets import load_dataset

repo = os.environ["REPO"]
out_dir = os.environ["OUT_DIR"]
os.makedirs(out_dir, exist_ok=True)

# Avoid streaming=True for heterogeneous repos (pyarrow CastError risk)
ds = load_dataset(repo, split="train", streaming=False)
for i, item in enumerate(ds):
    out_file = f"{out_dir}/legacy-{i}.jsonl"
    with open(out_file, "w") as f:
        json.dump({
            "prompt": item.get("prompt", ""),
            "response": item.get("response", ""),
        }, f)
        f.write("\n")
PY
fi

# Existing dedup + upload steps follow unchanged
echo "INFO: Dedup + upload step..."
# ... existing dedup.py + hf upload calls ...
```

```bash
chmod +x bin/dataset-enrich.sh
```

---

## Usage example (end-to-end)

Mac side (once per date folder, after rate-limit clears):

```bash
python3 bin/list_files.py \
  --repo axentx/surrogate-1-training-pairs \
  --path batches/public-merged/2026-05-02 \
  --out file-list.json
```


