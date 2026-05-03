# vanguard / discovery

# Final synthesized solution

## Diagnosis (merged)
- **Rate-limit & reproducibility**: Training/ingestion re-enumerate repos at runtime via repeated `list_repo_files`/`load_dataset` calls, causing HF API 429s and non-reproducible epochs.
- **Missing content-addressed snapshot**: No deterministic manifest (path + sha256 + size) per date folder, making CDN-only fetches and reproducible epochs impossible.
- **Schema hygiene**: Ingestion writes mixed-schema files instead of projecting to `{prompt,response}` and storing attribution in filenames.
- **Commit-cap & reuse**: No sibling-repo mapping to bypass HF commit cap (128/hr/repo) and no guard against recreating Lightning studios, wasting quota.

## Single proposed change
Add a lightweight, deterministic discovery-time manifest generator and update training to use CDN-only fetches with zero HF API calls during training.

Scope:
- New CLI: `/opt/axentx/vanguard/discovery/make_manifest.py`
- Optional sibling-repo mapping support for ingestion bursts
- Update training to accept `--manifest` and fetch via CDN (with sha256 verification)
- Add schema-projection wrapper for surrogate-1 ingestion

## Implementation

```bash
# /opt/axentx/vanguard/discovery/make_manifest.py
#!/usr/bin/env python3
"""
Generate content-addressed manifest for one date folder in a HuggingFace dataset repo.

Usage:
  python make_manifest.py --repo org/dataset --date 2026-04-29 --out ./manifests
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("ERROR: install huggingface_hub (pip install huggingface_hub)", file=sys.stderr)
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def build_manifest(repo_id: str, date_folder: str, out_dir: Path):
    api = HfApi()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Single API call: list only the requested folder (non-recursive)
    entries = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)

    manifest = {
        "repo": repo_id,
        "folder": date_folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [],
    }

    filelist_lines = []

    for e in entries:
        # Skip subfolders; include only files
        if getattr(e, "type", None) == "directory" or e.path.endswith(("/", "\\")):
            continue

        cdn_url = CDN_TEMPLATE.format(repo=repo_id, path=e.path)
        item = {
            "path": e.path,
            "size": getattr(e, "size", None),
            "sha256": getattr(e, "sha256", None),
            "cdn_url": cdn_url,
        }
        manifest["files"].append(item)
        filelist_lines.append(cdn_url)

    # Deterministic ordering for reproducibility
    manifest["files"].sort(key=lambda x: x["path"])
    filelist_lines.sort()

    stamp = date_folder.replace("/", "-")
    manifest_path = out_dir / f"manifest-{stamp}.json"
    list_path = out_dir / f"filelist-{stamp}.txt"

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    list_path.write_text("\n".join(filelist_lines) + "\n", encoding="utf-8")

    print(f"OK: {len(manifest['files'])} files -> {manifest_path} , {list_path}")
    return manifest_path, list_path


def main():
    parser = argparse.ArgumentParser(
        description="Create content-addressed manifest for HF dataset date folder"
    )
    parser.add_argument("--repo", required=True, help="HF dataset repo id (e.g., username/dataset)")
    parser.add_argument("--date", required=True, help="Date folder path in repo (e.g., 2026-04-29)")
    parser.add_argument("--out", default="./manifests", help="Output directory (default: ./manifests)")
    args = parser.parse_args()

    try:
        build_manifest(args.repo, args.date, Path(args.out))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

```bash
# Make executable
chmod +x /opt/axentx/vanguard/discovery/make_manifest.py

# /opt/axentx/vanguard/discovery/__init__.py (minimal)
__version__ = "0.1.0"
```

```python
# /opt/axentx/vanguard/train/train.py  (snippet to embed)
import json
import hashlib
from pathlib import Path
from huggingface_hub import hf_hub_download

def load_manifest(manifest_path: Path):
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)

def fetch_file(item, repo_id=None, cache_dir=None, verify_sha256=True):
    """
    Fetch a file via CDN with optional sha256 verification.
    Prefer hf_hub_download (cached) when repo_id+path available.
    """
    repo = repo_id or item.get("repo")
    path = item["path"]
    expected = item.get("sha256")

    # Use hf_hub_download for caching and auth handling when possible
    local_path = hf_hub_download(
        repo_id=repo,
        filename=path,
        cache_dir=cache_dir,
        force_download=False,
    )

    if verify_sha256 and expected:
        h = hashlib.sha256()
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        if h.hexdigest() != expected:
            raise ValueError(f"sha256 mismatch: {path}")
    return local_path

# In training setup:
#   parser.add_argument("--manifest", type=Path, help="Path to manifest JSON")
#   args = parser.parse_args()
#   manifest = load_manifest(args.manifest)
#   for item in manifest["files"]:
#       local = fetch_file(item, cache_dir="./cache")
#       ... process local file ...
```

```python
# /opt/axentx/vanguard/scripts/surrogate_ingest.py  (light wrapper)
"""
Project surrogate-1 schema to {prompt,response} and store attribution in filename.
Usage:
  python surrogate_ingest.py --src-dir ./enriched --out-dir ./projected
"""
import json
import uuid
from pathlib import Path

def project_record(rec: dict) -> dict:
    return {
        "prompt": rec.get("prompt") or rec.get("input") or "",
        "response": rec.get("response") or rec.get("output") or "",
    }

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--src-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    src = Path(args.src_dir)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    for f in src.rglob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            data = [data]

        projected = [project_record(r) for r in data]
        attribution = f.get("attribution", "unknown")
        run_id = str(uuid.uuid4())[:8]
        out_file = out / f"{attribution}_{run_id}.json"
        out_file.write_text(json.dumps(projected, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Projected files written to", out)

if __name__ == "__main__":
    main()
```

## Verification
1. Generate manifest (once per date folder):
   ```bash
   cd /opt/axentx/vanguard/discovery
   python make_manifest.py --repo my
