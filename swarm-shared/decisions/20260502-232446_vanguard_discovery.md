# vanguard / discovery

## 1. Diagnosis
- No persistent file manifest: repeated `list_repo_tree`/`load_dataset` calls during training will trigger HF API 429s.
- Lightning Studio reuse missing: each run likely recreates/stops studios, burning quota and risking idle-stop training loss.
- Schema drift risk: ingestion may write mixed schema into `enriched/`; surrogate-1 expects strict `{prompt,response}`.
- No CDN bypass strategy: training data loads still route through `/api/` auth endpoints instead of public CDN URLs.
- No deterministic shard-to-repo mapping: HF commit cap (128/hr/repo) could block ingestion bursts.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/file_manifest.py` (single new file) that:
- Runs once on the Mac (or CI) after rate-limit window clears.
- Calls `list_repo_tree(path, recursive=False)` for a date folder.
- Persists `file_manifest_{date}.json` with CDN URLs and repo metadata.
- Embeds deterministic shard→sibling repo mapping for writes.
- Exposes a loader that training scripts use for CDN-only fetches (zero API calls during train).

## 3. Implementation
```bash
# /opt/axentx/vanguard/discovery/file_manifest.py
#!/usr/bin/env python3
"""
Generate and use a persistent file manifest for HF datasets to avoid API rate limits.
Usage:
  python file_manifest.py --repo datasets/mycorp/vanguard-ingest --date 2026-05-02 --out manifest_2026-05-02.json
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

try:
    from huggingface_hub import list_repo_tree, hf_hub_download
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
SIBLING_REPOS = [
    "datasets/mycorp/vanguard-ingest",
    "datasets/mycorp/vanguard-ingest-s1",
    "datasets/mycorp/vanguard-ingest-s2",
    "datasets/mycorp/vanguard-ingest-s3",
    "datasets/mycorp/vanguard-ingest-s4",
    "datasets/mycorp/vanguard-ingest-s5",
]

def pick_sibling(slug: str) -> str:
    """Deterministic repo selection for commit-cap spreading."""
    digest = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(digest, 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def build_manifest(repo: str, date_folder: str, out_path: Path) -> Dict:
    """
    List top-level folder for date (recursive=False) and produce CDN manifest.
    """
    print(f"Listing {repo}/{date_folder} (recursive=False)...")
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=False)

    files = []
    for node in tree:
        if node.type != "file":
            continue
        path = f"{date_folder}/{node.path}" if date_folder else node.path
        files.append({
            "repo": repo,
            "path": path,
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=path),
            "size": getattr(node, "size", None),
            "lfs": getattr(node, "lfs", None),
        })

    manifest = {
        "repo": repo,
        "date": date_folder,
        "generated_by": "vanguard/discovery/file_manifest.py",
        "count": len(files),
        "files": files,
        "sibling_repos": SIBLING_REPOS,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written: {out_path} ({len(files)} files)")
    return manifest

def load_manifest(manifest_path: Path) -> Dict:
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)

def cdn_only_loader(manifest_path: Path):
    """
    Generator yielding (local_path, cdn_url) for Lightning training.
    Downloads via CDN (no HF API auth) to local cache then yields path.
    """
    manifest = load_manifest(manifest_path)
    cache_root = Path.home() / ".cache" / "vanguard" / "cdn"
    cache_root.mkdir(parents=True, exist_ok=True)

    for f in manifest["files"]:
        # Use hf_hub_download with local_dir to leverage CDN under the hood,
        # but avoid repeated list/tree calls during training.
        local = hf_hub_download(
            repo_id=f["repo"],
            filename=f["path"],
            cache_dir=str(cache_root),
            force_download=False,
            resume_download=True,
        )
        yield local, f["cdn_url"]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HF file manifest for CDN-only training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., datasets/xxx/yyy)")
    parser.add_argument("--date", required=True, help="Date folder to snapshot (e.g., 2026-05-02)")
    parser.add_argument("--out", default=None, help="Output JSON path (default: manifest_{date}.json)")
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path.cwd() / f"manifest_{args.date}.json"
    build_manifest(args.repo, args.date, out)
```

Update training launcher (example snippet to embed manifest):
```python
# train.py (or Lightning task) — minimal change
import json
from pathlib import Path

MANIFEST = Path("manifest_2026-05-02.json")
assert MANIFEST.exists(), "Generate manifest first via file_manifest.py"

with open(MANIFEST) as f:
    files = json.load(f)["files"]

# Use CDN URLs directly in your DataLoader (requests with no Authorization)
# or use hf_hub_download(cache_dir=...) as shown in cdn_only_loader().
# Zero HF API calls during training.
```

Lightning Studio reuse guard (add to launcher):
```python
from lightning import Teamspace, Studio, Machine

def get_or_create_studio(name: str):
    for s in Teamspace.studios():
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(
        name=name,
        machine=Machine.L40S,
        # Lightning-lambda-prod required for H200; fallback to public for L40S
    )
```

## 4. Verification
1. Generate manifest (Mac/CI):
   ```bash
   cd /opt/axentx/vanguard/discovery
   python file_manifest.py --repo datasets/mycorp/vanguard-ingest --date 2026-05-02 --out manifest_2026-05-02.json
   ```
   - Confirm JSON exists and `count > 0`.
   - Confirm each entry has valid `cdn_url` (curl -I one URL; expect 200, no auth).

2. CDN-only load test:
   ```bash
   python -c "
from file_manifest import cdn_only_loader
from pathlib import Path
for local, url in cdn_only_loader(Path('manifest_2026-05-02.json')):
    print('OK', local)
    break
"
   ```
   - Should print a local cache path without HF API errors.

3. Lightning Studio reuse:
   - Run launcher twice; second run should log `Reusing running studio` and not create a new studio.

4. Commit-cap spread (optional):
   - Call `pick_sibling("slug")` for multiple slugs; verify uniform distribution across `SIBLING_REPOS`.

5. Training run:
   - Start a Lightning Studio run with the updated `train.py` using the manifest.
   - Monitor HF API usage (should remain near zero during data load phase).
