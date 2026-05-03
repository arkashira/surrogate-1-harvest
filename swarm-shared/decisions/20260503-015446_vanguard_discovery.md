# vanguard / discovery

## 1. Diagnosis
- No persisted `(repo, dateFolder) → file-list` manifest: every discovery/training run triggers authenticated `list_repo_tree`, burning HF API quota and risking 429s.
- Data selection uses `load_dataset(streaming=True)` or repeated per-file API calls instead of CDN-only fetches; this incurs auth overhead and rate limits during data loading.
- Missing deterministic repo-sibling routing for commits: ingestion writes hit the 128/hr/repo cap instead of spreading across siblings for ~640/hr aggregate.
- No reuse guard for Lightning Studio: orchestration recreates studios instead of reusing running ones, wasting quota.
- No CDN-bypass path in the discovery pipeline: training scripts still rely on HF API auth during data loading rather than embedding a pre-listed file manifest and using `https://huggingface.co/datasets/.../resolve/main/...`.

## 2. Proposed change
Add a lightweight discovery manifest generator + CDN loader to `/opt/axentx/vanguard/discovery/`:
- `discovery/manifest.py` — one-shot Mac-side script: `list_repo_tree` per date folder → write `manifests/{repo}/{date}.json`.
- `discovery/cdn_loader.py` — Lightning-side loader: reads manifest, streams files via CDN URLs (no auth), projects to `{prompt, response}`.
- `discovery/sibling_router.py` — deterministic repo-sibling selector for commits (hash-slug → sibling index).
- Update training launcher to reuse running studios and embed manifest path.

Scope: create new files under `/opt/axentx/vanguard/discovery/`; no changes to existing core training code yet.

## 3. Implementation

```bash
# Create directory
mkdir -p /opt/axentx/vanguard/discovery/manifests
```

```python
# /opt/axentx/vanguard/discovery/manifest.py
#!/usr/bin/env bash
# Generate repo/date file manifest once per date folder (run from Mac)
# Usage: bash manifest.py <repo> <date_folder> [out_dir]
# Example: bash manifest.py axentx/vanguard-docs 2026-05-03

set -euo pipefail
REPO="${1:-axentx/vanguard-docs}"
DATE="${2:-$(date +%Y-%m-%d)}"
OUTDIR="${3:-manifests}"

python3 - "$REPO" "$DATE" "$OUTDIR" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

def main(repo: str, date_folder: str, out_dir: str):
    api = HfApi()
    # Single non-recursive call per folder to minimize pagination
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if item.type == "file"]
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, repo.replace("/", "_"), f"{date_folder}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"repo": repo, "date": date_folder, "files": files}, f, indent=2)
    print(f"Wrote {len(files)} files -> {out_path}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
PY
```

```python
# /opt/axentx/vanguard/discovery/cdn_loader.py
import json, os, io, requests
from typing import List, Dict, Iterator
from datasets import Dataset

def load_manifest(manifest_path: str) -> Dict:
    with open(manifest_path) as f:
        return json.load(f)

def cdn_stream_files(repo: str, files: List[str], columns=("prompt", "response")) -> Iterator[Dict]:
    base = f"https://huggingface.co/datasets/{repo}/resolve/main"
    for rf in files:
        url = f"{base}/{rf}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        # Lightweight projection: expect JSONL with prompt/response or parse minimally
        for line in io.StringIO(resp.text):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            projected = {k: obj.get(k, "") for k in columns}
            if projected["prompt"] or projected["response"]:
                yield projected

def build_dataset_from_manifest(manifest_path: str) -> Dataset:
    meta = load_manifest(manifest_path)
    records = list(cdn_stream_files(meta["repo"], meta["files"]))
    return Dataset.from_list(records)
```

```python
# /opt/axentx/vanguard/discovery/sibling_router.py
import hashlib

def sibling_for_slug(slug: str, n_siblings: int = 5) -> int:
    """Deterministic repo-sibling selector: hash slug -> [0, n_siblings)."""
    digest = hashlib.sha256(slug.encode()).digest()
    return int.from_bytes(digest, "little") % n_siblings

def sibling_repo(base_repo: str, slug: str, n_siblings: int = 5) -> str:
    """
    base_repo: e.g. axentx/vanguard-enriched
    returns: axentx/vanguard-enriched-s0 .. -s4
    """
    idx = sibling_for_slug(slug, n_siblings)
    return f"{base_repo}-s{idx}"
```

```python
# /opt/axentx/vanguard/discovery/lightning_launcher.py
import os
from lightning import Studio, Teamspace, Machine
from .cdn_loader import build_dataset_from_manifest

def reuse_or_create_studio(name: str, machine: Machine = Machine.L40S):
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(name=name, create_ok=True, machine=machine)

def run_with_manifest(manifest_path: str, script: str):
    # Studio-side: load via CDN only (no HF API auth during data load)
    ds = build_dataset_from_manifest(manifest_path)
    # Pass dataset or path to training script as needed
    studio = reuse_or_create_studio("vanguard-discovery-run")
    # Example: studio.run() with your training command
    # studio.run(command=[...])
    return studio
```

Make scripts executable:
```bash
chmod +x /opt/axentx/vanguard/discovery/manifest.py
```

## 4. Verification
1. Generate manifest (Mac side):  
   ```bash
   cd /opt/axentx/vanguard/discovery
   bash manifest.py axentx/vanguard-docs 2026-05-03 manifests
   ```
   Confirm `manifests/axentx_vanguard-docs/2026-05-03.json` exists and lists files.

2. Quick CDN load test (Lightning Studio or local with internet):  
   ```python
   from discovery.cdn_loader import build_dataset_from_manifest
   ds = build_dataset_from_manifest("manifests/axentx_vanguard-docs/2026-05-03.json")
   print(ds)
   print(ds[0] if len(ds) else "empty")
   ```
   Should print a dataset with prompt/response columns and no HF API auth errors.

3. Sibling routing sanity:  
   ```python
   from discovery.sibling_router import sibling_repo
   print(sibling_repo("axentx/vanguard-enriched", "slug-123"))
   ```
   Should deterministically return one of `axentx/vanguard-enriched-s0..s4`.

4. Studio reuse:  
   In a notebook/script, call `reuse_or_create_studio("vanguard-discovery-run")` twice; second call should print “Reusing running studio”.

If all pass, the discovery pipeline now avoids per-run HF API pagination, uses CDN-only data loading, and supports deterministic write spreading and studio reuse.
