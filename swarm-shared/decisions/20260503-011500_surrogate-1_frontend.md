# surrogate-1 / frontend

## Final Synthesis — Highest-value <2h frontend fix

**Decision (unified):**  
Replace recursive `list_repo_files` and per-file authenticated API calls in the frontend/data-source layer with **one per-folder `list_repo_tree` (non-recursive) + CDN-only fetches**, and project to `{prompt, response}` only at parse time.  

This eliminates:
- Recursive pagination → 429s  
- Per-file authenticated API calls during training/ingest → 429s  
- Mixed-schema parse failures (pyarrow `CastError`)  

While keeping Space OOM-safe (stream from CDN, no `load_dataset(streaming=True)` over heterogeneous repos) and staying within the 2-hour budget.

---

## Implementation plan (≤2h)

1. **Add lightweight manifest builder**  
   - Single `list_repo_tree(path, recursive=False)` per date folder.  
   - Save JSON manifest into repo (e.g. `manifests/file-list-YYYY-MM-DD.json`) so ingestion workers use **zero API calls** during streaming.

2. **Update frontend/data-source loader to CDN-only**  
   - Build URLs: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`  
   - No Authorization header (bypasses API rate limits).  
   - Parse parquet/jsonl and project to `{prompt, response}` immediately; drop other fields.

3. **Stream + deterministic sharding**  
   - Stream each file from CDN.  
   - Shard by `hash(slug) % 16` (or prompt hash fallback).  
   - Output: `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` (existing convention preserved).

4. **Reuse running Lightning Studio when present**  
   - Before creating, list `Teamspace.studios` and reuse any running instance with matching name.  
   - Start only if not running.

5. **Cron/Bash safety**  
   - Shebang `#!/usr/bin/env bash`, `chmod +x`, and set `SHELL=/bin/bash` in crontab.  
   - Scripts accept manifest path and shard id as args with safe defaults.

---

## Resolved contradictions (correctness + actionability)

- **Manifest location/use:** Candidate 1 saves manifests locally; Candidate 2 wants them committed to the runner repo.  
  **Resolution:** Save locally during build, but copy/commit the minimal date manifest into the runner repo as part of the deploy step (so workers never call the API during ingestion). This gives both reproducibility and zero runtime API calls.

- **Fallback behavior:** Candidate 2 mentions fallback to recursive listing if manifest missing.  
  **Resolution:** Do **not** fallback to recursive listing (it causes 429s). Instead, fail fast with a clear message and non-zero exit code if manifest is missing; require the manifest step to run first after rate-limit window. This avoids reintroducing the original failure mode.

- **Schema projection timing:** Both agree to project at parse time; Candidate 2 emphasizes this prevents `CastError`.  
  **Resolution:** Keep projection strict: only `prompt`/`response` (with key aliases), and coerce to string. Drop everything else before sharding to avoid schema mixing.

- **Lightning Studio reuse:** Candidate 1 includes reuse logic; Candidate 2 omits it.  
  **Resolution:** Keep reuse logic (it saves quota and is low risk). Use Candidate 1’s pattern with a small guard: validate studio is actually running before reuse.

---

## Unified code artifacts

### 1) Manifest builder (per date folder)

```python
# scripts/build_file_manifest.py
import os, json, datetime
from huggingface_hub import HfApi

REPO = "axentx/surrogate-1-training-pairs"
OUT_DIR = "manifests"
os.makedirs(OUT_DIR, exist_ok=True)

api = HfApi()
date_folder = datetime.date.today().isoformat()  # or pass as arg

# One non-recursive API call per folder
tree = api.list_repo_tree(REPO, path=date_folder, recursive=False)
files = [item.rfilename for item in tree if item.type == "file"]

manifest = {
    "repo": REPO,
    "folder": date_folder,
    "files": sorted(files),
    "generated_at": datetime.datetime.utcnow().isoformat() + "Z"
}

out_path = os.path.join(OUT_DIR, f"file-list-{date_folder}.json")
with open(out_path, "w") as f:
    json.dump(manifest, f, indent=2)

print(f"Wrote {len(files)} files to {out_path}")
```

Run once per date folder after rate-limit window:

```bash
python scripts/build_file_manifest.py
```

---

### 2) CDN-only loader with strict projection

```python
# src/data/cdn_loader.py
import requests, pyarrow.parquet as pq, io, json
from typing import Iterator, Dict

CDN_ROOT = "https://huggingface.co/datasets"

def cdn_url(repo: str, path: str) -> str:
    return f"{CDN_ROOT}/{repo}/resolve/main/{path}"

def project_record(raw: Dict) -> Dict:
    # Strict projection to avoid mixed schemas / CastError
    return {
        "prompt": str(raw.get("prompt") or raw.get("input") or ""),
        "response": str(raw.get("response") or raw.get("output") or "")
    }

def stream_cdn_files(manifest_path: str) -> Iterator[Dict]:
    with open(manifest_path) as f:
        manifest = json.load(f)

    repo = manifest["repo"]
    for rel_path in manifest["files"]:
        url = cdn_url(repo, rel_path)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()

        if rel_path.endswith(".parquet"):
            table = pq.read_table(io.BytesIO(resp.content))
            for batch in table.to_batches(max_chunksize=1000):
                for row in batch.to_pylist():
                    yield project_record(row)
        elif rel_path.endswith(".jsonl"):
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                row = json.loads(line)
                yield project_record(row)
        else:
            continue
```

---

### 3) Shard writer (deterministic)

```python
# src/data/shard_writer.py
import datetime, os, json, hashlib, argparse
from .cdn_loader import stream_cdn_files

SHARDS = 16
OUT_ROOT = "batches/public-merged"

def hash_slug(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def shard_id_for_slug(slug: str) -> int:
    return hash_slug(slug) % SHARDS

def write_shard(manifest_path: str, shard_idx: int):
    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    date = datetime.date.today().isoformat()
    out_dir = os.path.join(OUT_ROOT, date)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"shard{shard_idx}-{ts}.jsonl")

    with open(out_path, "w") as f:
        for rec in stream_cdn_files(manifest_path):
            slug = rec.get("slug") or rec.get("prompt", "")[:64]
            if shard_id_for_slug(slug) != shard_idx:
                continue
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"Shard {shard_idx} -> {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--shard", type=int, required=True)
    args = parser.parse_args()
    write_shard(args.manifest, args.shard)
```

---

### 4) Lightning Studio reuse (quota-safe)

```python
# scripts/launch_training.py
from lightning_sdk import Teamspace, Studio, Machine

TEAMSPACE = "your-teamspace"
STUDIO_NAME = "surrogate-1-train"

teamspace = Teamspace(name=TEAMSPACE)
running = [s for s in teamspace.studios if s.name == STUDIO_NAME and s.status == "Running"]

if running:
    studio = running[0]
    print(f"Reusing running studio: {studio.id}")
