# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Single Mac-side `list_repo_tree` call (once per date) → saves `manifest-{DATE}.json` to repo
- Worker loads manifest, keeps only items where `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads each file via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero HF API calls during ingestion, avoids 429/128-commit limits
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids PyArrow CastError)
- Dedup via centralized `lib/dedup.py` md5 store (shared across runners)
- Outputs `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with deterministic shard + timestamp
- Shebang `#!/usr/bin/env bash` wrapper retained for cron/workflow compatibility

---

### 1. Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py
"""
import os, sys, json, hashlib, time, datetime, subprocess
from pathlib import Path

import requests
import pyarrow.parquet as pq
import pyarrow as pa
from tqdm import tqdm

# ── config ──────────────────────────────────────────────────────────────
REPO_OWNER = os.getenv("HF_REPO_OWNER", "axentx")
REPO_NAME  = os.getenv("HF_REPO_NAME",  "surrogate-1-training-pairs")
BASE_URL   = f"https://huggingface.co/datasets/{REPO_OWNER}/{REPO_NAME}"

SHARD_ID    = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE        = os.getenv("DATE", datetime.date.today().isoformat())
HF_TOKEN    = os.getenv("HF_TOKEN", "")
OUT_DIR     = Path(f"batches/public-merged/{DATE}")
TIMESTAMP   = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE    = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ── dedup store ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore
dedup = DedupStore()

# ── helpers ─────────────────────────────────────────────────────────────
def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def belongs_to_shard(slug: str) -> bool:
    return slug_hash(slug) % SHARD_TOTAL == SHARD_ID

def load_manifest(date: str) -> list[str]:
    """Load manifest-{date}.json produced by Mac orchestration script."""
    manifest_path = Path(f"manifest-{date}.json")
    if not manifest_path.exists():
        print(f"[ERROR] Manifest not found: {manifest_path}", file=sys.stderr)
        print("Run Mac orchestration script first to generate manifest.", file=sys.stderr)
        sys.exit(1)
    with open(manifest_path) as f:
        return json.load(f)

def cdn_download(repo_path: str) -> bytes:
    """Download via CDN (no Authorization header -> bypass API rate limits)."""
    url = f"{BASE_URL}/resolve/main/{repo_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

def extract_pair(raw: dict) -> dict | None:
    """Project heterogeneous schemas to {prompt, response}."""
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
    if not prompt or not response:
        return None
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def process_parquet(content: bytes) -> list[dict]:
    """Read parquet bytes and extract pairs."""
    table = pq.read_table(pa.BufferReader(content))
    rows = table.to_pylist()
    out = []
    for row in rows:
        pair = extract_pair(row)
        if pair:
            out.append(pair)
    return out

def process_jsonl(content: bytes) -> list[dict]:
    out = []
    for line in content.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        pair = extract_pair(row)
        if pair:
            out.append(pair)
    return out

# ── main ────────────────────────────────────────────────────────────────
def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    items = load_manifest(DATE)

    # deterministic shard filter
    my_items = [p for p in items if belongs_to_shard(p)]
    print(f"[INFO] Shard {SHARD_ID}/{SHARD_TOTAL} -> {len(my_items)} items")

    written = 0
    with OUT_FILE.open("w") as fout:
        for repo_path in tqdm(my_items, desc="Ingesting"):
            try:
                data = cdn_download(repo_path)
                if repo_path.endswith(".parquet"):
                    pairs = process_parquet(data)
                elif repo_path.endswith(".jsonl"):
                    pairs = process_jsonl(data)
                else:
                    continue

                for pair in pairs:
                    # centralized dedup across runners
                    md5 = hashlib.md5(
                        json.dumps(pair, sort_keys=True).encode()
                    ).hexdigest()
                    if dedup.exists(md5):
                        continue
                    dedup.add(md5)
                    fout.write(json.dumps(pair, ensure_ascii=False) + "\n")
                    written += 1
            except Exception as exc:
                print(f"[WARN] {repo_path}: {exc}", file=sys.stderr)

    print(f"[DONE] Wrote {written} pairs -> {OUT_FILE}")

if __name__ == "__main__":
    main()
```

---

### 2. Create thin Bash wrapper `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
# Wrapper for cron / GitHub Actions.
# Required env: SHARD_ID, SHARD_TOTAL, DATE, HF_TOKEN
set -euo pipefail
export SHELL=/bin/bash

cd "$(dirname "$0")/.."
exec python3 bin/dataset-enrich.py "$@"
```

```bash
chmod +x bin/dataset-enrich.sh bin/dataset-enrich.py
```

---

### 3. Mac orchestration script (one-time per date) — `bin/gen-manifest.py`

```python
#!/usr/bin/env python3
"""
Generate manifest-{DATE}.json for surrogate-1-training-pairs.
Run once per date from Mac (or any machine) after HF API rate-limit window clears.
"""
import os, json, datetime
from huggingface_hub import HfApi

REPO_OWNER = os.getenv("HF_REPO_OWNER", "axentx")
REPO_NAME  = os.getenv("HF_REPO_NAME",  "surrogate-1-training-pairs")
DATE       = os.getenv("DATE", datetime.date.today().isoformat())
HF_TOKEN   = os.getenv("HF_TOKEN", "")

api = HfApi(token=HF_TOKEN)
# non-recursive per folder to avoid 100x pagination on huge repos
folders = ["batches/raw", "batches/mirror-merged"]  # adjust as needed
items = []
for folder in folders:
    try:
        entries = api.list_repo_tree(
            repo_id=f"{REPO_OWNER}/{REPO_NAME}",
            path=folder,
            repo_type="dataset",
            recursive=False,
        )
        for e in entries:
            if e.rfilename.endswith((".parquet", ".json
