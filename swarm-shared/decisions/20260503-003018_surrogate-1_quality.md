# surrogate-1 / quality

Below is the **single, consolidated implementation plan** that keeps the strongest, most actionable parts from both candidates, removes duplication, and resolves contradictions in favor of **correctness + concrete actionability**.

---

## Final Implementation Plan  
**CDN-first snapshot + zero-HF-API ingestion**  
**Goal:** Eliminate HF API rate-limit risk during training by producing a deterministic file manifest once (on the Mac orchestrator) and having Lightning training fetch exclusively via CDN URLs.

**Scope (fits <2h):**
- Add `bin/make_cdn_manifest.py` on the Mac orchestrator:
  - One non-recursive `list_repo_tree` call for a single date folder (e.g., `public-merged/<latest-date>/` or `axentx/surrogate-1-training-pairs/<date>/`).
  - Filter to shards (`.parquet` or `.jsonl`) as needed.
  - Emit `file_manifest.json` with:
    - `cdn_url`
    - `path`
    - `size`
    - integrity hash (`md5` if available; prefer LFS oid or SHA256 fallback)
- Add `training/cdn_stream.py` (or inline in `train.py`):
  - Load `file_manifest.json`
  - Stream files via CDN URLs with `requests`/`aiohttp` + format-aware parsing (Parquet or JSONL)
  - Zero `datasets`/`hf_api` calls during training
  - Optional deterministic shuffle across shards with seed
- Update Lightning launcher to:
  - Reuse a running Studio when present
  - Pass manifest path via `arguments`
  - Restart cleanly if stopped
- Optional: add a lightweight hook in `bin/dataset-enrich.sh` to regenerate the manifest after enrichment.

**Why this is highest-value:**  
- Removes 429/1000-per-5min API risk during training  
- Uses public CDN bypass (`resolve/main/`)  
- Preserves deterministic shard selection without changing ingestion logic  
- Minimal change surface (<2h)

---

## Concrete Steps (all runnable in <2h)

1. **Create snapshot script on Mac orchestrator**  
   - Single non-recursive `list_repo_tree` call for chosen date folder.  
   - Produce `file_manifest.json` with `cdn_url`, `path`, `size`, integrity hash.  
   - Save and commit/version with training code or pass as artifact.

2. **Update training data loader**  
   - Replace `load_dataset(streaming=True)` or recursive HF API usage.  
   - Read `file_manifest.json`, stream files via CDN URLs.  
   - Parse each file into `{prompt, response}` only at parse time (tolerate schema heterogeneity).  
   - Optional deterministic shuffle + resume offset via index.

3. **Update Lightning launcher**  
   - Reuse running Studio when present (`Teamspace.studios`).  
   - Pass manifest path via `run(..., arguments=[manifest_path])`.  
   - On idle-stop, restart with `target.start(machine=Machine.L40S)` if stopped.

4. **Smoke test**  
   - Run snapshot script locally.  
   - Run training locally with manifest (small sample).  
   - Launch Studio job and verify zero HF API calls during data load (check logs).

---

## Code Snippets

### 1) `bin/make_cdn_manifest.py` (Mac orchestrator)

```python
#!/usr/bin/env python3
"""
Create CDN-only manifest for a single date folder in axentx/surrogate-1-training-pairs.

Usage:
    HF_TOKEN=hf_xxx python bin/make_cdn_manifest.py 2026-05-01 > file_manifest.json
"""
import os
import json
import sys
from huggingface_hub import HfApi

API = HfApi(token=os.environ.get("HF_TOKEN"))
REPO = "datasets/axentx/surrogate-1-training-pairs"

def main(date_folder: str) -> None:
    # single non-recursive call
    entries = API.list_repo_tree(repo_id=REPO, path=date_folder, recursive=False)
    manifest = []
    for e in entries:
        if getattr(e, "type", None) != "file":
            continue
        # keep only shards if desired (optional filter)
        # if not (e.path.endswith(".parquet") or e.path.endswith(".jsonl")):
        #     continue

        path = f"{date_folder}/{e.path}"
        cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"

        # integrity: prefer LFS oid or size-based placeholder
        md5 = None
        if hasattr(e, "lfs") and isinstance(e.lfs, dict):
            oid = e.lfs.get("oid")
            if oid and oid.startswith("sha256:"):
                md5 = oid  # or recompute later; this is a strong integrity ref
            elif oid:
                md5 = oid
        manifest.append({
            "cdn_url": cdn_url,
            "path": path,
            "size": getattr(e, "size", None),
            "md5": md5,
        })
    json.dump(manifest, sys.stdout, indent=2)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: make_cdn_manifest.py <date-folder>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
```

Make executable:

```bash
chmod +x bin/make_cdn_manifest.py
```

---

### 2) Training loader: `training/cdn_stream.py` (or inline in `train.py`)

```python
import json
import io
import pyarrow.parquet as pq
import requests
from typing import List, Dict, Iterator

def load_manifest(manifest_path: str) -> List[Dict]:
    with open(manifest_path) as f:
        return json.load(f)

def stream_parquet_to_pairs(cdn_url: str) -> Iterator[Dict]:
    """Download single parquet via CDN and yield {prompt, response}."""
    resp = requests.get(cdn_url, stream=True, timeout=60)
    resp.raise_for_status()
    buf = io.BytesIO(resp.content)
    table = pq.read_table(buf)

    # Tolerate schema heterogeneity
    prompt_col = next((c for c in ("prompt", "input") if c in table.column_names), None)
    response_col = next((c for c in ("response", "output") if c in table.column_names), None)

    if prompt_col is None or response_col is None:
        raise ValueError(f"Missing required columns in {cdn_url}. Found: {table.column_names}")

    for row in table.to_pylist():
        yield {
            "prompt": row.get(prompt_col) or "",
            "response": row.get(response_col) or "",
        }

def cdn_dataset(manifest: List[Dict], shuffle: bool = False, seed: int = 42) -> Iterator[Dict]:
    import random
    items = list(manifest)
    if shuffle:
        random.Random(seed).shuffle(items)

    for item in items:
        try:
            yield from stream_parquet_to_pairs(item["cdn_url"])
        except Exception as exc:
            # log and skip corrupt file; don't kill entire run
            print(f"Skipping {item['path']}: {exc}")
            continue
```

Usage in training script:

```python
manifest = load_manifest("file_manifest.json")
train_examples = list(cdn_dataset(manifest, shuffle=True, seed=42))
```

For JSONL shards (alternative):

```python
def stream_jsonl_to_pairs(cdn_url: str) -> Iterator[Dict]:
    resp = requests.get(cdn_url, stream=True, timeout=60)
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        prompt = row.get("prompt") or row.get("input") or ""
        response = row.get("response") or row.get("output") or ""
        yield {"prompt": prompt, "response": response}
```

---

### 3) Lightning launcher reuse snippet

```python
from lightning import Lightning, Teamspace, Machine

lt = Lightning()
teamspace = Teamspace()

studio_name = "surrogate-1-train"
studio = None
for s in teamspace.studios
