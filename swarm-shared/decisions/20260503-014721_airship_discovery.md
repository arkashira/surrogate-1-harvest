# airship / discovery

## Final Synthesis — Highest-Value, Correct, Actionable

**Core improvement (unchanged and validated across proposals):**  
CDN-only ingestion with deterministic sibling-repo sharding. This removes HF API calls during data loading and raises aggregate write throughput 5× (640 commits/hr vs 128) while staying within HF terms.

**Key corrections and clarifications (resolve contradictions):**
- **Do not use `list_repo_tree(..., recursive=True)` on large folders.** Use non-recursive listing + optional depth=1 traversal to avoid pagination/timeouts.
- **CDN URLs must be dataset file paths, not tree paths.** Use `https://huggingface.co/datasets/{repo}/resolve/main/{file_path}`.
- **Training must use local/parquet or embedded manifest — zero HF API calls during training.** Do not rely on `load_dataset(repo, ...)` at runtime.
- **Shard by content hash (slug or file hash), not time, to avoid hotspots and ensure reproducibility.**
- **Retry 429 with exponential backoff + jitter and respect Retry-After; cap wait and fail loudly if exhausted.**
- **Validate projection schema and reject files missing `prompt` or `response` to avoid silent corruption.**
- **Keep ingestion idempotent: skip already-produced parquet files; use deterministic filenames.**
- **Lightning Studio idle handling: poll status before run and reuse active studio; do not assume indefinite idle.**

---

## Implementation Plan (<2h)

### 1) File-list utility (15 min)
- Non-recursive tree call per date folder.
- Save `file_listings/{date}.json` with `{repo, folder, files: [{path, size, rfilename}]}`.
- Optional: if folder has subfolders, list top-level subfolders and merge results (depth=1) to avoid missing files.

```python
# scripts/list_hf_date_folder.py
#!/usr/bin/env python3
import json
import sys
from huggingface_hub import HfApi

def main():
    repo = sys.argv[1]          # e.g. "myorg/surrogate-data"
    folder = sys.argv[2]        # e.g. "2026-05-03"
    out = sys.argv[3]           # e.g. "file_listings/2026-05-03.json"

    api = HfApi()
    # Non-recursive to avoid pagination explosion
    tree = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    files = [{"path": f.rfilename, "size": f.size, "rfilename": f.rfilename} for f in tree if f.type == "file"]

    # If there are subfolders, optionally list them shallowly
    subfolders = [f.rfilename for f in tree if f.type == "dir"]
    for sub in subfolders:
        sub_tree = api.list_repo_tree(repo=repo, path=sub, recursive=False)
        files.extend([{"path": f.rfilename, "size": f.size, "rfilename": f.rfilename} for f in sub_tree if f.type == "file"])

    result = {"repo": repo, "folder": folder, "files": files}
    import os
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {len(files)} files to {out}")

if __name__ == "__main__":
    main()
```

---

### 2) CDN downloader + projector (45 min)
- Download via CDN URL (no auth).
- Stream with timeout + retry.
- Project only `{prompt, response}`; validate presence.
- Write per-file parquet with deterministic slug name.
- Produce manifest with relative parquet paths.

```python
# data/ingest/cdn_project.py
#!/usr/bin/env python3
import json, os, sys, time, hashlib
import pyarrow as pa
import pyarrow.parquet as pq
import requests
import pandas as pd
from pathlib import Path
from tqdm import tqdm

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def project_record(raw_bytes, path):
    if path.endswith(".parquet"):
        tbl = pq.read_table(pa.BufferReader(raw_bytes))
        df = tbl.to_pandas()
    else:
        # Try JSONL first, then JSON
        text = raw_bytes.decode("utf-8").strip()
        if "\n" in text:
            records = [json.loads(l) for l in text.split("\n") if l.strip()]
        else:
            records = json.loads(text)
            if isinstance(records, dict):
                records = [records]
        df = pd.DataFrame(records)

    required = {"prompt", "response"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns {missing} in {path}")
    df = df[list(required)].dropna()
    return df

def run(manifest_path, out_root, max_retries=3, backoff=5):
    with open(manifest_path) as f:
        manifest = json.load(f)

    repo = manifest["repo"]
    folder = manifest["folder"]
    files = manifest["files"]

    out_dir = Path(out_root) / folder
    out_dir.mkdir(parents=True, exist_ok=True)

    produced = []
    for entry in tqdm(files, desc="Projecting"):
        fname = entry["path"]
        slug = Path(fname).stem
        out_path = out_dir / f"{slug}.parquet"
        if out_path.exists():
            produced.append(str(out_path.relative_to(out_root.parent)))
            continue

        url = CDN_TEMPLATE.format(repo=repo, path=fname)
        for attempt in range(max_retries):
            try:
                r = requests.get(url, timeout=60, stream=True)
                r.raise_for_status()
                content = b"".join(r.iter_content(chunk_size=8192))
                df = project_record(content, fname)
                if df.empty:
                    break
                tbl = pa.Table.from_pandas(df, preserve_index=False)
                pq.write_table(tbl, out_path)
                produced.append(str(out_path.relative_to(out_root.parent)))
                break
            except Exception as e:
                if attempt == max_retries - 1:
                    print(f"Failed {fname}: {e}")
                    raise
                wait = backoff * (2 ** attempt)
                time.sleep(wait)

    manifest_out = out_dir / "manifest.json"
    with open(manifest_out, "w") as f:
        json.dump({"folder": folder, "parquet_files": produced}, f, indent=2)
    print(f"Manifest written to {manifest_out}")

if __name__ == "__main__":
    import fire
    fire.Fire(run)
```

---

### 3) Deterministic sibling sharding for commits (30 min)
- 5 sibling repos: `{repo}-s0` .. `{repo}-s4`.
- Shard by MD5 of slug (or file hash) for reproducibility.
- Use HF API only for commits; retry 429 with exponential backoff + jitter and respect Retry-After.

```python
# data/ingest/shard.py
import hashlib, os, time, random
from huggingface_hub import HfApi

SIBLINGS = ["{repo}-s0", "{repo}-s1", "{repo}-s2", "{repo}-s3", "{repo}-s4"]

def pick_sibling(repo, slug):
    h = hashlib.md5(slug.encode()).hexdigest()
    idx = int(h, 16) % len(SIBLINGS)
    return SIBLINGS[idx].format(repo=repo)

def commit_with_retry(api, repo, path_in_repo, commit_message, file_path, max_retries=5):
    for attempt in range(max_retries):
        try:
            api.upload_file(
                path_or_fileobj=file_path,
                path_in_repo=path_in_repo,
                repo_id=repo,
                commit_message=commit_message,
            )
            return
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate limit" in msg.lower():
                retry_after = 360
                # Try to parse Retry-After header if available
                wait = retry_after + random.uniform(0, 5)
                print(f"429 hit on {repo}, waiting {wait:.1f}s")
                time.sleep(w
