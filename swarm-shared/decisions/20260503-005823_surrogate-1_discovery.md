# surrogate-1 / discovery

Below is the **single, merged implementation** that keeps every high-leverage idea from both proposals while removing duplication and contradictions.  
It is organized for <2h shipping, correctness, and concrete actionability.

---

## 1) Core strategy (what we keep from both)
- **CDN-bypass ingestion** (no HF API auth/rate limits during downloads).  
- **Deterministic shard→repo routing** across 5 siblings to beat 128-commit/hour cap (Candidate 2).  
- **Single API list call → snapshot JSON** reused by training (Candidate 1 + Candidate 2).  
- **Zero-API training path**: training reads snapshot and downloads via CDN only.  
- **Cross-run dedup** via SQLite to prevent duplicates across shards/runs (Candidate 2).  
- **Schema projection** to `{prompt, response}` and per-pair hashing (Candidate 2).

---

## 2) Deterministic routing (lib/routing.py)
```python
# lib/routing.py
import hashlib

SIBLINGS = [
    "axentx/surrogate-1-training-pairs",
    "axentx/surrogate-1-training-pairs-sib1",
    "axentx/surrogate-1-training-pairs-sib2",
    "axentx/surrogate-1-training-pairs-sib3",
    "axentx/surrogate-1-training-pairs-sib4",
]

def pick_repo(slug: str) -> str:
    """Deterministic repo assignment from slug."""
    digest = hashlib.md5(slug.encode()).hexdigest()
    idx = int(digest, 16) % len(SIBLINGS)
    return SIBLINGS[idx]

def pick_repo_by_shard(shard_id: int, total_shards: int = 16) -> str:
    """Map N shards into 5 siblings deterministically."""
    idx = shard_id % len(SIBLINGS)
    return SIBLINGS[idx]
```

---

## 3) Cross-run dedup store (lib/dedup.py)
```python
# lib/dedup.py
import sqlite3
from pathlib import Path
from contextlib import contextmanager

class DedupStore:
    def __init__(self, db_path: str = ".dedup.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS seen_pair (hash TEXT PRIMARY KEY)")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            yield conn
        finally:
            conn.close()

    def add(self, pair_hash: str) -> bool:
        """Return True if newly inserted, False if duplicate."""
        cur = self._conn().__enter__().cursor()
        try:
            cur.execute("INSERT INTO seen_pair (hash) VALUES (?)", (pair_hash,))
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            cur.close()

    def bulk_contains(self, hashes):
        if not hashes:
            return set()
        cur = self._conn().__enter__().cursor()
        cur.execute(f"SELECT hash FROM seen_pair WHERE hash IN ({','.join('?'*len(hashes))})", hashes)
        return {row[0] for row in cur.fetchall()}
```

---

## 4) Worker: CDN-only, schema projection, dedup (bin/dataset-enrich-worker.py)
```python
#!/usr/bin/env python3
# bin/dataset-enrich-worker.py
import argparse
import hashlib
import json
import os
import requests
import pyarrow.parquet as pq
from pathlib import Path
from lib.dedup import DedupStore

HF_BASE = "https://huggingface.co/datasets"

def cdn_url(repo: str, path: str) -> str:
    return f"{HF_BASE}/{repo}/resolve/main/{path}"

def hash_pair(prompt: str, response: str) -> str:
    return hashlib.md5(f"{prompt}\0{response}".encode()).hexdigest()

def normalize_record(rec):
    prompt = rec.get("prompt") or rec.get("input") or rec.get("question") or ""
    response = rec.get("response") or rec.get("output") or rec.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--total-shards", type=int, default=16)
    parser.add_argument("--date", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--dedup-db", default=".dedup.db")
    args = parser.parse_args()

    with open(args.snapshot) as f:
        files = json.load(f)

    shard_files = [f for i, f in enumerate(files) if i % args.total_shards == args.shard_id]
    dedup = DedupStore(args.dedup_db)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    written = 0

    with open(args.out, "w") as out_f:
        for rel_path in shard_files:
            url = cdn_url(args.target_repo, rel_path)
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
            except Exception as exc:
                print(f"Skip {rel_path}: {exc}")
                continue

            tmp = Path("/tmp") / f"tmp_{os.getpid()}.parquet"
            tmp.write_bytes(resp.content)
            try:
                table = pq.read_table(tmp)
                for batch in table.to_batches():
                    for rec in batch.to_pylist():
                        nr = normalize_record(rec)
                        if not nr["prompt"] or not nr["response"]:
                            continue
                        h = hash_pair(nr["prompt"], nr["response"])
                        if dedup.add(h):
                            out_f.write(json.dumps(nr, ensure_ascii=False) + "\n")
                            written += 1
            finally:
                tmp.unlink(missing_ok=True)

    print(f"Shard {args.shard_id}: wrote {written} unique pairs to {args.out}")

if __name__ == "__main__":
    main()
```

---

## 5) Updated dataset-enrich.sh (bin/dataset-enrich.sh)
```bash
#!/usr/bin/env bash
set -euo pipefail

# Required env: HF_TOKEN, SHARD_ID (0-15), RUN_DATE (YYYY-MM-DD)
HF_USER="axentx"
DATASET="surrogate-1-training-pairs"
DATE="${RUN_DATE:-$(date +%Y-%m-%d)}"
TS="$(date +%H%M%S)"
OUT="batches/public-merged/${DATE}/shard${SHARD_ID}-${TS}.jsonl"

# Deterministic sibling selection (16 shards -> 5 siblings)
SIBLING_IDX=$(( SHARD_ID % 5 ))
case $SIBLING_IDX in
  0) TARGET_REPO="${HF_USER}/${DATASET}" ;;
  1) TARGET_REPO="${HF_USER}/${DATASET}-sib1" ;;
  2) TARGET_REPO="${HF_USER}/${DATASET}-sib2" ;;
  3) TARGET_REPO="${HF_USER}/${DATASET}-sib3" ;;
  4) TARGET_REPO="${HF_USER}/${DATASET}-sib4" ;;
esac

echo "Using target repo: ${TARGET_REPO}"

# Single API call: list date folder only (non-recursive)
FOLDER_SNAPSHOT="file-list-${DATE}-shard${SHARD_ID}.json"
python -c "
import json, os
from huggingface_hub import list_repo_tree
files = list_repo_tree('${HF_USER}/${DATASET}', path='${DATE}', recursive=False, repo_type='dataset')
with open('${FOLDER_SNAPSHOT}', 'w') as f:
    json.dump([f.rfilename for f in files], f)
"

# Run worker (CDN-only downloads, schema projection, dedup)
python bin/dataset-enrich-worker.py \
  --shard-id "$SHARD_ID" \
  --total-shards 16 \
  --date "$DATE" \
  --snapshot "$FOLDER_S
