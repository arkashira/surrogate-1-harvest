# airship / discovery

## Implementation Plan: Deterministic CDN-Only Discovery Orchestrator

**Scope**: Harden `airship discover` into a deterministic, CDN-only orchestrator that eliminates HF API rate limits and PyArrow schema errors while producing reproducible file lists and artifact manifests in <2h.

**Deliverables**:
1. `scripts/discover-cdn-list.py` — Mac-safe orchestrator: single HF API tree call → local JSON file list → embed in training runtime.
2. `scripts/train-cdn-only.py` — Lightning Studio training script: zero HF API calls during data load; CDN-only fetches via `hf_hub_download`/`resolve/main/` with per-file schema projection.
3. `scripts/build-manifest.sh` — Deterministic manifest builder: hash-slug → repo shard, deterministic filenames, no mixed-schema columns.
4. `docker-compose.discovery.yml` — Optional local runner (CPU-only) for validation.

---

### 1) File list generation (Mac orchestration)

```python
# scripts/discover-cdn-list.py
#!/usr/bin/env python3
"""
Generate deterministic CDN file list for a date folder.
Run on Mac (or any orchestrator) after rate-limit window clears.

Usage:
  python discover-cdn-list.py \
    --repo datasets/your-repo \
    --date 2026-04-29 \
    --out filelist-2026-04-29.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

# Avoid accidental local HF API abuse; prefer CDN.
# We still need one tree call per date folder — do it sparingly.
try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

def list_date_folder(repo_id: str, date: str):
    api = HfApi()
    prefix = f"{date}/"
    # Non-recursive per folder to avoid 100x pagination and 429.
    tree = api.list_repo_tree(repo_id=repo_id, path=prefix, recursive=False)
    files = [
        {"path": f.rfilename, "size": getattr(f, "size", None)}
        for f in tree
        if not f.rfilename.endswith("/")
    ]
    return sorted(files, key=lambda x: x["path"])

def main():
    parser = argparse.ArgumentParser(description="Generate CDN file list.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/repo)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    files = list_date_folder(args.repo, args.date)
    manifest = {
        "repo": args.repo,
        "date": args.date,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
        "note": "CDN-only. Use resolve/main/ URLs to bypass HF API rate limits."
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x scripts/discover-cdn-list.py
```

---

### 2) Lightning training script (CDN-only, zero HF API runtime)

```python
# scripts/train-cdn-only.py
"""
Lightning Studio training script.
- Uses CDN-only file list (embedded or passed via filelist.json).
- No load_dataset(streaming=True) on heterogeneous repos.
- Per-file hf_hub_download or direct CDN fetch; project to {prompt,response}.
- Deterministic shard naming: batches/mirror-merged/{date}/{slug}.parquet
"""
import json
import os
import hashlib
import pyarrow as pa
import pyarrow.parquet as pq
from pathlib import Path
from typing import List, Dict

# Prefer direct CDN downloads to avoid HF API auth/rate limits.
# CDN: https://huggingface.co/datasets/{repo}/resolve/main/{path}
try:
    import requests
except ImportError:
    requests = None

try:
    from huggingface_hub import hf_hub_download
except ImportError:
    hf_hub_download = None

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def deterministic_slug(file_path: str, content_hint: str = "") -> str:
    h = hashlib.sha256()
    h.update(file_path.encode("utf-8"))
    if content_hint:
        h.update(content_hint.encode("utf-8"))
    return h.hexdigest()[:16]

def pick_repo_shard(slug: str, siblings: int = 5) -> int:
    """Deterministic sibling repo assignment to bypass HF commit cap."""
    return int(slug[:8], 16) % siblings

def project_to_pair(raw) -> Dict[str, str]:
    """
    Convert heterogeneous file content to {prompt, response}.
    Implement per-extension as needed. Examples:
    - JSONL: expect 'prompt'/'response' or 'instruction'/'output'
    - JSON: try common keys
    - Text: treat first block as prompt, remainder as response
    """
    # Minimal safe placeholder — extend for your schemas.
    if isinstance(raw, dict):
        prompt = raw.get("prompt") or raw.get("instruction") or raw.get("input") or ""
        response = raw.get("response") or raw.get("output") or raw.get("completion") or ""
        return {"prompt": str(prompt), "response": str(response)}
    return {"prompt": "", "response": str(raw)}

def fetch_file(repo: str, path: str, use_cdn: bool = True, local_cache: str = "./.cache") -> str:
    local_cache = Path(local_cache)
    local_cache.mkdir(parents=True, exist_ok=True)
    local_path = local_cache / path.replace("/", "_")
    if local_path.exists():
        return str(local_path)

    if use_cdn and requests:
        url = CDN_TEMPLATE.format(repo=repo, path=path)
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        local_path.write_bytes(r.content)
        return str(local_path)

    if hf_hub_download:
        p = hf_hub_download(repo_id=repo, filename=path, cache_dir=str(local_cache))
        return p

    raise RuntimeError("No fetch method available.")

def build_parquet_for_date(filelist_path: str, date: str, repo: str, out_root: str = "batches/mirror-merged"):
    with open(filelist_path) as f:
        manifest = json.load(f)

    rows = []
    for entry in manifest["files"]:
        path = entry["path"]
        try:
            local_file = fetch_file(repo, path, use_cdn=True)
            # Lightweight parse: implement per-format loader as needed.
            # Example for JSONL:
            suffix = Path(path).suffix.lower()
            if suffix == ".jsonl":
                import json as _json
                with open(local_file) as fp:
                    for line in fp:
                        line = line.strip()
                        if not line:
                            continue
                        raw = _json.loads(line)
                        pair = project_to_pair(raw)
                        rows.append(pair)
            elif suffix == ".json":
                import json as _json
                with open(local_file) as fp:
                    raw = _json.load(fp)
                    if isinstance(raw, list):
                        for item in raw:
                            rows.append(project_to_pair(item))
                    else:
                        rows.append(project_to_pair(raw))
            else:
                # Fallback: treat whole file as single text blob
                text = Path(local_file).read_text(encoding="utf-8", errors="replace")
                # crude split: first 1/3 prompt, rest response
                pivot = max(1, len(text) // 3)
                rows.append({"prompt": text[:pivot], "response": text[pivot:]})
        except Exception as e:
            print(f"Skipping {path} due to error: {e}")
            continue

    if not rows:
        print("No rows produced.")
        return

    table = pa.Table.from_pylist(rows, schema=pa.schema([
       
