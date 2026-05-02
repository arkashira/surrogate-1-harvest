# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Replace runtime `load_dataset(streaming=True)` + recursive `list_repo_files` in `bin/dataset-enrich.sh` with deterministic pre-flight snapshot + CDN-only fetches. This eliminates HF API rate limits, pyarrow CastError on mixed schemas, and reduces per-shard memory pressure while preserving the 16-shard parallel ingest design.

### Steps (1h 30m total)

1. **Add snapshot generator** (`bin/make-snapshot.py`) — run once per date folder from Mac (or cron before the 16 runners start). Uses `list_repo_tree(path, recursive=False)` per subfolder to avoid 100× pagination and 429s. Outputs `snapshot-{date}.json` containing `{repo, path, sha, size}` for every file. (20m)
2. **Update `bin/dataset-enrich.sh`** — accept snapshot file as optional arg. If provided, iterate over snapshot entries instead of calling `datasets.load_dataset`. Download each file via raw CDN URL (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with `wget`/`curl` + streaming JSONL projection to `{prompt,response}`. Remove `datasets` usage for listing/streaming heterogeneous repos. (45m)
3. **Update `.github/workflows/ingest.yml`** to compute deterministic shard assignment from snapshot entries (not repo file list) and pass snapshot artifact to each job. (20m)
4. **Add lightweight projection util** (`bin/project_pair.py`) — parse each downloaded file (json/jsonl/parquet) and yield only `{prompt,response}` + md5 hash. Keep memory bounded via iterators. (20m)
5. **Smoke test** one shard locally; verify no `datasets` streaming calls remain and CDN-only fetches work. (10m)

---

### Code snippets

#### 1) bin/make-snapshot.py
```python
#!/usr/bin/env python3
"""
Create a deterministic snapshot for a date folder in axentx/surrogate-1-training-pairs.
Usage:
  python bin/make-snapshot.py --date 2026-05-02 --out snapshot-2026-05-02.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
CDN_ROOT = "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    api = HfApi()
    # Single non-recursive call per date folder (avoids 429 from recursive listing)
    entries = api.list_repo_tree(repo_id=HF_REPO, path=args.date, recursive=False)

    snapshot = []
    for entry in entries:
        if entry.type != "file":
            continue
        # Only include files this shard cares about (parquet/json/jsonl)
        if not entry.path.lower().endswith((".parquet", ".json", ".jsonl")):
            continue
        snapshot.append(
            {
                "repo": HF_REPO,
                "path": entry.path,               # repo-relative path
                "sha": entry.commit_id or "",
                "size": entry.size or 0,
                "cdn_url": f"{CDN_ROOT}/{entry.path}",
            }
        )

    payload = {
        "date": args.date,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "snapshot": snapshot,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(snapshot)} files to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/make-snapshot.py
```

---

#### 2) bin/project_pair.py
```python
#!/usr/bin/env python3
"""
Project heterogeneous files to {prompt,response} + md5 hash.
Memory-bounded streaming for json/jsonl/parquet.
"""
import hashlib
import json
import sys
from pathlib import Path
from typing import Iterator, Tuple

import pyarrow.parquet as pq

def md5_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()

def iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)

def project_file(path: Path) -> Iterator[Tuple[str, str, str]]:
    """Yield (prompt, response, md5_of_raw_bytes)"""
    suffix = path.suffix.lower()
    raw = path.read_bytes()
    digest = md5_bytes(raw)

    if suffix == ".parquet":
        # Avoid loading entire file into memory; use pyarrow streaming reader
        with pq.ParquetFile(path) as pf:
            for batch in pf.iter_batches(batch_size=1024):
                df = batch.to_pandas()
                for _, row in df.iterrows():
                    prompt = str(row.get("prompt") or row.get("input") or "")
                    response = str(row.get("response") or row.get("output") or "")
                    if prompt or response:
                        yield prompt, response, digest
    else:
        # Assume json/jsonl
        for obj in iter_jsonl(path):
            prompt = str(obj.get("prompt") or obj.get("input") or "")
            response = str(obj.get("response") or obj.get("output") or "")
            if prompt or response:
                yield prompt, response, digest

def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: project_pair.py <file>", file=sys.stderr)
        sys.exit(1)

    path = Path(sys.argv[1])
    for prompt, response, digest in project_file(path):
        print(json.dumps({"prompt": prompt, "response": response, "md5": digest}, ensure_ascii=False))

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/project_pair.py
```

---

#### 3) bin/dataset-enrich.sh (updated core loop)
```bash
#!/usr/bin/env bash
# Updated: deterministic snapshot + CDN-only fetches
set -euo pipefail
# Ensure consistent environment for cron/actions
export SHELL=/bin/bash

HF_REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date -u +%Y-%m-%d)}"
SNAPSHOT="${2:-snapshot-${DATE}.json}"
SHARD_ID="${SHARD_ID:-0}"
SHARD_TOTAL="${SHARD_TOTAL:-16}"
WORKDIR=$(mktemp -d)
cd "$WORKDIR"

cleanup() {
  cd /
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "[$(date -u)] Shard $SHARD_ID/$SHARD_TOTAL | date=$DATE | snapshot=$SNAPSHOT"

# Fetch snapshot (artifact or pre-generated file)
if [[ ! -f "$SNAPSHOT" ]]; then
  echo "Snapshot not found: $SNAPSHOT" >&2
  exit 1
fi

# Deterministic shard assignment by stable index
mapfile -t ENTRIES < <(
  python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
for idx, item in enumerate(data['snapshot']):
    print(idx, item['cdn_url'])
" "$SNAPSHOT"
)

# Filter entries for this shard
for line in "${ENTRIES[@]}"; do
  idx=$(echo "$line" | awk '{print $1}')
  url=$(echo "$line" | awk '{print $2}')
  if (( idx % SHARD_TOTAL != SHARD_ID )); then
    continue
  fi

  fname=$(basename "$url")
  echo "[$(date -u)] Processing shard entry $idx/$url"

  # CDN
