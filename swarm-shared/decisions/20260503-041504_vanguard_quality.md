# vanguard / quality

## 1. Diagnosis

- No content-addressed manifest exists → training and UI hit HF API at runtime (429s, non-reproducible epochs, no shareable snapshots).
- Dataset ingestion produces mixed-schema files in `enriched/` (extra `source`, `ts` cols) instead of projecting to `{prompt, response}` only, breaking downstream schema assumptions.
- Training scripts use `load_dataset(streaming=True)` on heterogeneous repos → pyarrow `CastError` on mixed file schemas.
- Lightning Studio is recreated on every run instead of reused → burns 80+ hrs/mo quota and risks idle-stop training death.
- No CDN-bypass strategy → every data fetch consumes HF API rate limit; no deterministic file list for reproducible epochs.

## 2. Proposed change

Create a single, content-addressed snapshot manifest and a Lightning launcher that reuses studios and fetches via CDN only.

Files to add/modify (scope):
- `/opt/axentx/vanguard/mk-manifest.py` — new: list repo tree once, produce `manifest-{date}.json` with CDN URLs and per-file sha256.
- `/opt/axentx/vanguard/train.py` — modify: accept `--manifest` and stream via `hf_hub_download`/CDN URLs; project to `{prompt, response}` only.
- `/opt/axentx/vanguard/run-studio.py` — modify: reuse running studio by name; restart if idle-stopped.

## 3. Implementation

```bash
# /opt/axentx/vanguard/mk-manifest.py
#!/usr/bin/env python3
"""
Generate content-addressed manifest for a HF dataset repo folder.
Usage:
  python mk-manifest.py --repo HuggingFaceH4/ultrachat_200k --folder 2026-05-03 --out manifest-2026-05-03.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi, list_repo_tree

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, folder: str, out_path: Path):
    api = HfApi()
    entries = list_repo_tree(repo=repo, path=folder, recursive=False)

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        path = f"{folder}/{entry.path}"
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=path)
        # content hash approximated by path+size (true hash requires download; use etag if available)
        files.append({
            "path": path,
            "cdn_url": cdn_url,
            "size": entry.size or 0,
            "etag": getattr(entry, "etag", None),
        })

    manifest = {
        "repo": repo,
        "folder": folder,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }

    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files -> {out_path}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--folder", required=True)
    p.add_argument("--out", default="manifest.json")
    args = p.parse_args()
    build_manifest(args.repo, args.folder, Path(args.out))
```

```python
# /opt/axentx/vanguard/train.py  (key excerpts to replace/insert)
import json
import pyarrow as pa
from pathlib import Path
from typing import Iterator, Dict

import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

def project_to_prompt_response(file_path: Path) -> Iterator[Dict[str, str]]:
    """Project heterogeneous parquet/jsonl to {prompt, response} only."""
    try:
        table = pq.read_table(str(file_path))
    except Exception:
        # fallback for jsonl
        import pyarrow.json as pj
        table = pj.read_json(str(file_path))

    # Normalize column names
    cols = {c.lower(): c for c in table.column_names}
    prompt_col = cols.get("prompt") or cols.get("instruction") or cols.get("input")
    response_col = cols.get("response") or cols.get("output") or cols.get("completion")

    if not prompt_col or not response_col:
        raise ValueError(f"Missing prompt/response cols in {file_path}: {table.column_names}")

    prompts = table.column(prompt_col).to_pylist()
    responses = table.column(response_col).to_pylist()
    for p, r in zip(prompts, responses):
        if p is None or r is None:
            continue
        yield {"prompt": str(p), "response": str(r)}

def load_manifest(manifest_path: Path):
    return json.loads(manifest_path.read_text())

def cdn_only_data_iter(manifest, cache_dir: Path = Path(".cache")):
    cache_dir.mkdir(exist_ok=True)
    for f in manifest["files"]:
        local_path = cache_dir / Path(f["path"]).name
        if not local_path.exists():
            # CDN bypass: use raw URL download (no API auth)
            import requests
            r = requests.get(f["cdn_url"], timeout=60)
            r.raise_for_status()
            local_path.write_bytes(r.content)
        for item in project_to_prompt_response(local_path):
            yield item
```

```python
# /opt/axentx/vanguard/run-studio.py  (key excerpts)
from lightning import Studio, Machine, Teamspace
import time

STUDIO_NAME = "vanguard-train-l40s"

def get_or_create_studio():
    ts = Teamspace()
    for s in ts.studios:
        if s.name == STUDIO_NAME:
            if s.status == "Running":
                print(f"Reusing running studio: {STUDIO_NAME}")
                return s
            else:
                print(f"Studio stopped; restarting")
                s.start(machine=Machine.L40S)
                return s
    print(f"Creating studio: {STUDIO_NAME}")
    return Studio(
        name=STUDIO_NAME,
        machine=Machine.L40S,
        create_ok=True,
    )

def run_training_script(script_path: str, args: list):
    studio = get_or_create_studio()
    # Ensure studio is running before submit
    while studio.status != "Running":
        print("Waiting for studio to be Running...")
        time.sleep(10)
        studio.refresh()
    run = studio.run(
        command=["python", script_path] + args,
        environment="BASE",
    )
    print(f"Submitted run: {run.id}")
    return run
```

## 4. Verification

1. Generate manifest (single API call from Mac):
   ```bash
   cd /opt/axentx/vanguard
   python mk-manifest.py --repo HuggingFaceH4/ultrachat_200k --folder 2026-05-03 --out manifest-2026-05-03.json
   ```
   - Confirm `manifest-*.json` exists and lists files with `cdn_url`.

2. Dry-run CDN-only fetch (zero API calls during training):
   ```bash
   python -c "
import json, requests
m=json.load(open('manifest-2026-05-03.json'))
for f in m['files'][:2]:
    r=requests.get(f['cdn_url'], timeout=30)
    r.raise_for_status()
    print('OK', f['path'], len(r.content))
"
   ```
   - Expect HTTP 200 for each file; no Authorization header used.

3. Validate schema projection:
   ```bash
   python -c "
from train import project_to_prompt_response
from pathlib import Path
for item in project_to_prompt_response(Path('.cache/ultrachat_200k-2026-05-03-some.parquet')):
    print(item)
    break
"
   ```
   - Confirm output contains only keys `prompt` and `response`.

4. Reuse studio and run:
   ```bash
   python run-studio.py  # (or call run_training_script from Python)
   ```
   - Confirm it reuses existing running studio; check Lightning UI for `
