# vanguard / discovery

## 1. Diagnosis

- No content-addressed manifest exists → frontend/training cannot select deterministic snapshots and re-runs risk 429s from runtime HF API calls.
- Data loading likely uses `load_dataset` or `list_repo_tree` at runtime (in frontend or training code) instead of CDN-only paths, causing rate-limit exposure.
- No local file listing persisted after the rate-limit window clears → each run re-queries HF API instead of embedding a static file list.
- No clear separation between orchestration (Mac) and compute (Lightning) — risk of running model code locally.
- Missing verification that CDN URLs resolve and match expected sha256 — integrity is not enforced.

## 2. Proposed change

Create a single canonical manifest generator under `/opt/axentx/vanguard` that:
- Runs once on the Mac (orchestration) after rate-limit clears.
- Uses `list_repo_tree` once per date folder, saves a content-addressed JSON manifest with `sha256`, `cdn_url`, and `slug`.
- Embeds that manifest path into training/frontend so all downstream loads are CDN-only with zero HF API calls.

Scope:
- Add `/opt/axentx/vanguard/scripts/build_manifest.py`
- Add `/opt/axentx/vanguard/manifests/` (gitignored) for outputs.
- Update any loader stub to accept `--manifest` and use CDN-only fetches.

## 3. Implementation

```bash
# Ensure project structure
mkdir -p /opt/axentx/vanguard/{scripts,manifests}
touch /opt/axentx/vanguard/.gitignore && echo "manifests/" >> /opt/axentx/vanguard/.gitignore
```

`/opt/axentx/vanguard/scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Build a content-addressed manifest for a HuggingFace dataset repo folder.
Usage:
  HF_TOKEN=<token> python build_manifest.py \
    --repo "datasets/mycorp/vanguard-data" \
    --folder "batches/mirror-merged/2026-05-03" \
    --out "/opt/axentx/vanguard/manifests/manifest-2026-05-03.json"
"""

import argparse
import hashlib
import json
import os
import sys
from typing import List, Dict

from huggingface_hub import HfApi, list_repo_tree

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def build_manifest(repo: str, folder: str, out_path: str) -> None:
    api = HfApi(token=os.getenv("HF_TOKEN"))
    entries = list_repo_tree(
        repo=repo,
        path=folder,
        recursive=False,
        token=api.token,
    )

    files: List[Dict] = []
    for entry in entries:
        if entry.type != "file":
            continue
        # CDN download (no auth header required for public datasets) to capture sha256 once.
        # If repo is private, pass token to requests via stream.
        url = CDN_TEMPLATE.format(repo=repo, path=entry.path)
        import requests
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        h = hashlib.sha256()
        for chunk in resp.iter_content(chunk_size=8192):
            h.update(chunk)
        digest = h.hexdigest()

        files.append({
            "slug": entry.path,
            "sha256": digest,
            "cdn_url": url,
            "size": entry.size if hasattr(entry, "size") else None,
        })

    manifest = {
        "repo": repo,
        "folder": folder,
        "generated_by": "vanguard/scripts/build_manifest.py",
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    print(f"Manifest written to {out_path} ({len(files)} files)")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN-only manifest for HF dataset folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., datasets/mycorp/vanguard-data)")
    parser.add_argument("--folder", required=True, help="Folder inside repo (e.g., batches/mirror-merged/2026-05-03)")
    parser.add_argument("--out", required=True, help="Output JSON manifest path")
    args = parser.parse_args()

    try:
        build_manifest(args.repo, args.folder, args.out)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

Example stub loader for training/frontend (non-breaking):
`/opt/axentx/vanguard/scripts/load_cdn_only.py`
```python
#!/usr/bin/env python3
"""
Load parquet shards from a manifest using CDN URLs only (zero HF API calls).
"""
import json
import pyarrow.parquet as pq
import requests
from typing import List, Dict

def load_from_manifest(manifest_path: str):
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    rows = []
    for item in manifest["files"]:
        if not item["slug"].endswith(".parquet"):
            continue
        # Streaming read from CDN
        resp = requests.get(item["cdn_url"], stream=True, timeout=60)
        resp.raise_for_status()
        with open("/tmp/temp.parquet", "wb") as tmp:
            for chunk in resp.iter_content(chunk_size=8192):
                tmp.write(chunk)
        table = pq.read_table("/tmp/temp.parquet", columns=["prompt", "response"])
        rows.append(table.to_pylist())
    return rows
```

## 4. Verification

1. Run manifest build (once per snapshot):
   ```bash
   cd /opt/axentx/vanguard
   HF_TOKEN=hf_xxx python scripts/build_manifest.py \
     --repo "datasets/mycorp/vanguard-data" \
     --folder "batches/mirror-merged/2026-05-03" \
     --out "manifests/manifest-2026-05-03.json"
   ```
   - Confirm `manifests/manifest-2026-05-03.json` exists and contains `files[]` with `sha256` and `cdn_url`.

2. CDN-only load test:
   ```bash
   python scripts/load_cdn_only.py manifests/manifest-2026-05-03.json
   ```
   - Confirm rows load without any `huggingface_hub` imports or API calls (check via `lsof` or `strace -e trace=network`).

3. Integrity check:
   ```bash
   python -c "
import json, hashlib, requests
m = json.load(open('manifests/manifest-2026-05-03.json'))
for f in m['files']:
    r = requests.get(f['cdn_url'], timeout=30)
    d = hashlib.sha256(r.content).hexdigest()
    assert d == f['sha256'], f['slug']
print('OK')
"
   ```
   - All sha256 must match.

4. Rate-limit safety:
   - While iterating the manifest, run `grep -r 'list_repo_tree\|load_dataset' scripts/` — should return nothing used in hot paths.
   - Confirm no HF API calls during load by monitoring traffic or running with `HF_HUB_DISABLE_TELEMETRY=1` and observing no 429s.
