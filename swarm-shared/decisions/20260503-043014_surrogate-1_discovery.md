# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/worker.py`** — deterministic shard worker:
   - Accepts `SHARD_ID`/`N_SHARDS` (0–15) via env/args.
   - Uses a **pre-generated `manifest.json`** (committed by dev) to avoid any HF listing API calls in CI.
   - If manifest missing, falls back to a single `list_repo_tree(..., recursive=False)` for today’s folder (or most recent non-empty folder) and persists `manifest-shard-<id>.json` locally.
   - Downloads only assigned shard’s files via **CDN bypass** (`resolve/main/...`), no auth/API calls.
   - Projects heterogeneous files to `{prompt, response}` at parse time (avoids `load_dataset`/pyarrow CastError).
   - Deduplicates via centralized `lib/dedup.py` md5 store.
   - Writes `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

2. **Add `bin/gen-manifest.py`** (run on Mac/dev machine) — pre-list once, embed:
   - Calls `list_repo_tree(path, recursive=False)` per date folder (non-recursive to avoid 429).
   - Emits `manifest.json` mapping `slug → cdn_url` and `slug → shard_id` (hash-based).
   - Commits or uploads alongside workflow so runners never call HF listing APIs.

3. **Update `.github/workflows/ingest.yml`**:
   - Matrix `shard_id: [0..15]`.
   - Each job runs `python bin/worker.py --shard $SHARD_ID --manifest manifest.json`.
   - No recursive `list_repo_files`; uses CDN-only fetches.
   - Retries with exponential backoff on CDN 429/5xx.

4. **Update `lib/dedup.py`** (if needed):
   - Ensure thread/process-safe SQLite access (WAL mode) for concurrent workers.

5. **Remove/Deprecate** shell-heavy parts of `dataset-enrich.sh` that invoke `load_dataset` or recursive listing.

---

### Code Snippets

#### `bin/gen-manifest.py` (run on Mac)

```python
#!/usr/bin/env python3
"""
Generate manifest.json for surrogate-1 public dataset ingestion.
Run from Mac (or any dev machine) after HF API rate-limit window clears.
"""
import json, os, hashlib
from huggingface_hub import list_repo_tree

REPO = "axentx/surrogate-1-training-pairs"
OUT = "manifest.json"
N_SHARDS = 16

def shard_id(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % N_SHARDS

def main():
    manifest = {"shards": {str(i): [] for i in range(N_SHARDS)}, "files": {}}
    # One folder per date: iterate top-level non-recursive
    for item in list_repo_tree(REPO, path="", recursive=False):
        if item.type != "directory":
            continue
        date = item.path
        for f in list_repo_tree(REPO, path=date, recursive=False):
            if f.type != "file":
                continue
            slug = f"{date}/{f.path}"
            sid = shard_id(slug)
            entry = {
                "slug": slug,
                "cdn_url": f"https://huggingface.co/datasets/{REPO}/resolve/main/{slug}",
                "shard": sid,
            }
            manifest["shards"][str(sid)].append(entry)
            manifest["files"][slug] = entry

    os.makedirs(os.path.dirname(OUT) if os.path.dirname(OUT) else ".", exist_ok=True)
    with open(OUT, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Wrote {OUT} with {len(manifest['files'])} files across {N_SHARDS} shards.")

if __name__ == "__main__":
    main()
```

#### `bin/worker.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass worker for a single shard.
Usage: python bin/worker.py --shard 3 --manifest manifest.json
"""
import argparse, json, hashlib, os, sys, time, requests
from pathlib import Path
from typing import Dict, Any

# Add repo root to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import is_duplicate, mark_seen  # noqa
from huggingface_hub import list_repo_tree

N_RETRIES = 5
BACKOFF = 5

def parse_to_pair(raw: bytes, slug: str) -> Dict[str, str]:
    """
    Project heterogeneous file to {prompt, response}.
    Extend with format-specific logic as needed.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        lines = raw.decode("utf-8").strip().splitlines()
        if len(lines) == 1:
            data = json.loads(lines[0])
        else:
            data = {"prompt": lines[0] if len(lines) > 0 else "", "response": lines[1] if len(lines) > 1 else ""}

    prompt = data.get("prompt") or data.get("input") or data.get("question") or ""
    response = data.get("response") or data.get("output") or data.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response), "slug": slug}

def download_cdn(url: str) -> bytes:
    for attempt in range(N_RETRIES):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 429:
                wait = BACKOFF * (2 ** attempt)
                print(f"CDN 429, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            if attempt == N_RETRIES - 1:
                raise
            time.sleep(BACKOFF * (2 ** attempt))
    raise RuntimeError("Unreachable")

def build_manifest_fallback(shard_id: int, n_shards: int) -> list:
    """
    Fallback: list today's folder (or most recent non-empty) and build entries for this shard.
    """
    REPO = "axentx/surrogate-1-training-pairs"
    # Find most recent non-empty date folder
    date_folders = []
    for item in list_repo_tree(REPO, path="", recursive=False):
        if item.type == "directory":
            date_folders.append(item.path)
    date_folders.sort(reverse=True)

    entries = []
    for date in date_folders:
        files = list_repo_tree(REPO, path=date, recursive=False)
        if not files:
            continue
        for f in files:
            if f.type != "file":
                continue
            slug = f"{date}/{f.path}"
            sid = int(hashlib.md5(slug.encode()).hexdigest(), 16) % n_shards
            if sid == shard_id:
                entries.append({
                    "slug": slug,
                    "cdn_url": f"https://huggingface.co/datasets/{REPO}/resolve/main/{slug}",
                })
        if entries:
            break
    return entries

def worker(shard_id: int, manifest_path: str, n_shards: int = 16, out_root: str = "batches/public-merged"):
    entries = []
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
        entries = manifest["shards"].get(str(shard_id), [])
    else:
        entries = build_manifest_fallback(shard_id, n_shards)

    if not entries:
        print(f"No files for shard {shard_id}")
        return

    date = time.strftime("%Y-%m-%d")
    out_dir = Path(out_root) / date
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    out_file = out_dir / f"shard{shard
