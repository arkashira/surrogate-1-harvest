# vanguard / discovery

## Final Consolidated Implementation

### Diagnosis (merged, contradictions resolved)
- **Quota burn**: every training/data-selection launch performs authenticated `list_repo_tree` instead of a single snapshot → 429s.
- **No CDN bypass**: training uses API-backed loading instead of deterministic CDN-only fetches.
- **Missing artifact**: no persisted `(repo, date_folder) → file-list` manifest to decouple discovery (once) from training (CDN-only).
- **No guardrails**: no validation of manifest before training and no lightweight CLI to produce/consume manifests.
- **Scope discipline**: keep changes minimal (<2h), avoid speculative studio-reuse logic that introduces coupling and new failure modes.

### Proposed Change (merged, strongest parts)
- Add one discovery utility that lists repo tree **once**, persists a deterministic manifest, and prints its path.
- Manifest schema: `repo`, `date_folder`, `generated_at`, `files[]`, `count`, `cdn_prefix`.
- Create `/opt/axentx/vanguard/manifests/` and `/opt/axentx/vanguard/scripts/discover_filelist.py`.
- Provide a minimal training-side helper to consume the manifest and construct CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{filepath}`) with no auth during data loading.
- Add validation (non-empty files, correct repo/path structure) before training starts.
- Keep launcher/train changes minimal: accept `--filelist` or `HF_FILELIST_MANIFEST` and switch to CDN-only fetches.

---

### Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/manifests
mkdir -p /opt/axentx/vanguard/scripts
```

Create `/opt/axentx/vanguard/scripts/discover_filelist.py`:

```python
#!/usr/bin/env python3
"""
Generate a CDN-only file-list manifest for a repo + date folder.

Usage:
  python discover_filelist.py <repo> <date_folder> [--out-dir DIR]

Example:
  python discover_filelist.py datasets/myorg/surrogate-1 2026-04-29

Produces:
  vanguard/manifests/myorg--surrogate-1/2026-04-29.json
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("ERROR: huggingface_hub not installed. Install with: pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)


def repo_slug(repo: str) -> str:
    # datasets/myorg/surrogate-1 -> myorg--surrogate-1
    return repo.replace("/", "--").replace(".", "_")


def validate_repo_path(repo: str, date_folder: str) -> None:
    if not repo or not date_folder:
        raise ValueError("repo and date_folder must be non-empty strings.")
    if " " in repo or " " in date_folder:
        raise ValueError("repo and date_folder must not contain spaces.")


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <repo> <date_folder> [--out-dir DIR]", file=sys.stderr)
        sys.exit(1)

    repo = sys.argv[1]
    date_folder = sys.argv[2]

    out_dir = Path(__file__).parent.parent / "manifests"
    if len(sys.argv) > 4 and sys.argv[3] == "--out-dir":
        out_dir = Path(sys.argv[4])

    try:
        validate_repo_path(repo, date_folder)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    api = HfApi()
    print(f"Listing {repo}/{date_folder} ...", file=sys.stderr)

    try:
        items = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    except Exception as exc:
        print(f"ERROR listing repo tree: {exc}", file=sys.stderr)
        sys.exit(1)

    # Normalize items to file paths
    files = []
    for item in items:
        if isinstance(item, dict):
            path = item.get("path")
        else:
            path = getattr(item, "path", None)
        if path:
            files.append(path)

    if not files:
        print("ERROR: no files found for the given repo/date_folder. Manifest must be non-empty.", file=sys.stderr)
        sys.exit(1)

    cdn_prefix = f"https://huggingface.co/datasets/{repo}/resolve/main"
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": sorted(set(files)),
        "count": len(files),
        "cdn_prefix": cdn_prefix,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / repo_slug(repo) / f"{date_folder}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(str(out_path))


if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/scripts/discover_filelist.py
```

Create a minimal training helper `/opt/axentx/vanguard/scripts/train_with_manifest.py` (example usage):

```python
#!/usr/bin/env python3
"""
Minimal helper to load a manifest and build CDN URLs for training.

Usage:
  python train_with_manifest.py --filelist PATH [--limit N]

Environment:
  HF_FILELIST_MANIFEST  optional path to manifest (overridden by --filelist)
"""

import argparse
import json
import sys
from pathlib import Path


def load_manifest(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"Manifest not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    required = {"repo", "date_folder", "files", "cdn_prefix"}
    if not required.issubset(manifest):
        missing = required - set(manifest)
        raise ValueError(f"Manifest missing required fields: {missing}")
    if not isinstance(manifest["files"], list) or not manifest["files"]:
        raise ValueError("Manifest must contain non-empty 'files' list.")
    return manifest


def build_urls(manifest):
    prefix = manifest["cdn_prefix"].rstrip("/")
    return [f"{prefix}/{f}" for f in manifest["files"]]


def main():
    parser = argparse.ArgumentParser(description="Train using a CDN-only manifest.")
    parser.add_argument("--filelist", type=Path, help="Path to manifest JSON")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of files (for smoke tests)")
    args = parser.parse_args()

    manifest_path = args.filelist or Path(os.environ.get("HF_FILELIST_MANIFEST", ""))
    if not manifest_path or not manifest_path.is_file():
        print("ERROR: provide --filelist or set HF_FILELIST_MANIFEST to a valid manifest.", file=sys.stderr)
        sys.exit(1)

    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        print(f"ERROR: invalid manifest: {exc}", file=sys.stderr)
        sys.exit(1)

    urls = build_urls(manifest)
    if args.limit:
        urls = urls[:args.limit]

    print(f"Loaded {len(urls)} files from manifest: {manifest_path}")
    # Replace this stub with your actual data loader / Lightning dataset.
    # Example: dataset = YourCdnDataset(file_urls=urls, ...)
    for u in urls[:3]:
        print("  ->", u)
    print("Ready for training (CDN-only).")


if __name__ == "__main__":
    import os
    main()
```

```bash
chmod +x /opt/axentx/vanguard/scripts/train_with_manifest.py
```

---

### Verification (concrete, actionable)

1. **Generate manifest** (replace with a repo you can list):
   ```bash
   python /opt/axentx/v
