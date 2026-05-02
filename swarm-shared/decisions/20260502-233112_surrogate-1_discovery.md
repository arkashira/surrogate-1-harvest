# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Replace runtime `load_dataset(streaming=True)` + recursive `list_repo_tree` in `bin/dataset-enrich.sh` with a **deterministic pre-flight snapshot + CDN-only fetches**. This eliminates HF API rate limits (429), pyarrow `CastError` from mixed schemas, and reduces per-shard memory pressure while keeping 16 shards fully parallel with zero API calls during data load.

### Steps (est. 90–110 min)

1. **Add snapshot generator** (`bin/make-snapshot.py`) — run once per cron tick (or manually) from the orchestrator/Mac. Uses `list_repo_tree(path, recursive=False)` per date folder to avoid 429. Emits `snapshot/<date>/file-list.json` with `{repo, path, sha, size, slug}`. Deterministic shard assignment via `hash(slug) % 16 == SHARD_ID`. (20–25 min)

2. **Update `bin/dataset-enrich.sh`** — accept snapshot path arg: `SNAPSHOT_FILE`. Read assigned files from snapshot (jq). Fetch each file via CDN URL:  
   `https://huggingface.co/datasets/$REPO/resolve/main/$PATH`  
   (no Authorization header → bypasses API rate limit). Stream-parse, project to `{prompt, response}`, dedup via inline hashing, append to `shard-<N>-<ts>.jsonl`. Preserve existing upload logic to `batches/public-merged/<date>/`. (30–35 min)

3. **Update workflow** (`/.github/workflows/ingest.yml`) — add optional `snapshot-file` input (default: auto-computed from date). Pass `SNAPSHOT_FILE` to each matrix job. Keep 16-shard matrix unchanged. (10 min)

4. **Validation & rollback** — dry-run snapshot locally; verify shard counts sum to total files. Keep old codepath behind `USE_LEGACY_LOAD=1` for quick rollback. (10–15 min)

5. **Add CDN loader helper** (`bin/lib/cdn_loader.py`) — lightweight stream-downloader/parser used by the shell script to normalize schemas and emit canonical `{hash, prompt, response}`. Keeps parsing logic maintainable and testable. (20–25 min)

---

## Code Snippets

### 1. Snapshot generator (`bin/make-snapshot.py`)

```python
#!/usr/bin/env python3
"""
Generate deterministic file-list snapshot for surrogate-1 dataset.
Usage:
  HF_TOKEN=<token> python bin/make-snapshot.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out snapshot/2026-05-03/file-list.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi, list_repo_tree

def stable_slug(filename: str) -> str:
    return os.path.splitext(os.path.basename(filename))[0]

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder in dataset")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    token = os.getenv("HF_TOKEN")
    if not token:
        print("HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    api = HfApi(token=token)
    root = args.date  # e.g. "2026-05-03"
    entries = list_repo_tree(
        repo_id=args.repo,
        path=root,
        recursive=False,
        repo_type="dataset",
    )

    files = []
    for e in entries:
        if e.type != "file":
            continue
        slug = stable_slug(e.path)
        files.append(
            {
                "repo": args.repo,
                "path": e.path,
                "sha": e.lfs.get("oid", None) if getattr(e, "lfs", None) else None,
                "size": e.size,
                "slug": slug,
            }
        )

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": args.repo,
        "date": args.date,
        "root": root,
        "count": len(files),
        "files": sorted(files, key=lambda x: x["path"]),
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

### 2. CDN loader helper (`bin/lib/cdn_loader.py`)

```python
#!/usr/bin/env python3
"""
Stream CDN file and emit canonical {hash, prompt, response} JSONL lines.
Designed to be used in a shell pipeline:
  curl ... | python bin/lib/cdn_loader.py
"""
import json
import hashlib
import sys
from typing import Any, Dict, Iterable, Optional

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pyarrow.json as pj
    _ARROW_OK = True
except Exception:  # pragma: no cover
    _ARROW_OK = False


def canonical_hash(prompt: str, response: str) -> str:
    return hashlib.md5((prompt.strip() + "\n" + response.strip()).encode()).hexdigest()


def normalize_record(obj: Dict[str, Any]) -> Optional[Dict[str, str]]:
    prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
    response = obj.get("response") or obj.get("output") or ""
    if not prompt or not response:
        return None
    return {
        "hash": canonical_hash(prompt, response),
        "prompt": prompt.strip(),
        "response": response.strip(),
    }


def stream_jsonl(lines: Iterable[bytes]) -> Iterable[Dict[str, str]]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        rec = normalize_record(obj)
        if rec:
            yield rec


def stream_parquet(content: bytes) -> Iterable[Dict[str, str]]:
    if not _ARROW_OK:
        return
    try:
        table = pq.read_table(pa.BufferReader(content))
        # Project only likely fields; tolerate missing columns
        fields = set(table.column_names)
        prompt_col = next((c for c in ("prompt", "input", "text") if c in fields), None)
        response_col = next((c for c in ("response", "output") if c in fields), None)
        if not prompt_col or not response_col:
            return
        prompts = table[prompt_col].to_pylist()
        responses = table[response_col].to_pylist()
        for p, r in zip(prompts, responses):
            rec = normalize_record({"prompt": p or "", "response": r or ""})
            if rec:
                yield rec
    except Exception:
        return


def stream_arrow_json(content: bytes) -> Iterable[Dict[str, str]]:
    if not _ARROW_OK:
        return
    try:
        table = pj.read_json(pa.BufferReader(content))
        fields = set(table.column_names)
        prompt_col = next((c for c in ("prompt", "input", "text") if c in fields), None)
        response_col = next((c for c in ("response", "output") if c in fields), None)
        if not prompt_col or not response_col:
            return
        prompts = table[prompt_col].to_pylist()
        responses = table[response_col].to_pylist()
        for p, r in zip(prompts, responses):
            rec = normalize_record({"prompt": p or "", "response": r or ""})
            if rec:
                yield rec
    except Exception:
        return


def main() -> None:
    # Detect input type by first non-empty chunk extension hint (passed via env) or content sniff.
    # Simpler: rely on caller to
