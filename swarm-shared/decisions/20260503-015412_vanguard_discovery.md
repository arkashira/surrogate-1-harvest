# vanguard / discovery

# Final Synthesized Solution

## 1. Diagnosis (Consensus)
All candidates agree on the core bottlenecks:

- **No file-list cache**: every run triggers authenticated `list_repo_tree`, burning HF quota and risking 429s.
- **No CDN bypass**: training uses `load_dataset(streaming=True)` or per-file API calls instead of anonymous CDN fetches, causing auth-bound rate limits during data loading.
- **No deterministic repo assignment**: single repo ingestion hits HF commit cap (128/hr); no sharding across sibling repos to scale writes.
- **No Lightning Studio reuse**: training scripts create new studios instead of reusing running ones, wasting quota.
- **No idle-stop resilience**: Lightning idle timeout kills training; no pre-run status check + restart.

## 2. Implementation Plan (Single Deliverable)
Create **one new file** and **minimal edits** to launcher/train script:

- `/opt/axentx/vanguard/discovery/file_manifest.py` (cache builder + CDN loader + sharding + studio helpers)
- Update launcher to call `file_manifest.py build` once per `(repo, dateFolder)` (on Mac or after rate-limit window).
- Update training script to:
  - Load manifest and use **CDN-only URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) with zero auth.
  - Use deterministic sibling-repo sharding for writes.
  - Reuse running Lightning Studio and restart if idle-stopped.

If no training script exists, provide a minimal `train.py` stub that uses the manifest.

## 3. Final Code

### `/opt/axentx/vanguard/discovery/file_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate and use CDN-only file manifests for HF datasets.
Usage:
  python file_manifest.py build --repo <repo> --date <YYYY-MM-DD> --out-dir manifests/
  python file_manifest.py train --repo <repo> --date <YYYY-MM-DD> --script train.py
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

MANIFEST_DIR = Path(__file__).parent.parent / "manifests"

# Sibling repos for sharding writes (adjust to your org)
SIBLING_REPOS = [
    "vanguard-enriched-0",
    "vanguard-enriched-1",
    "vanguard-enriched-2",
    "vanguard-enriched-3",
    "vanguard-enriched-4",
]

def pick_sibling_repo(slug: str) -> str:
    """Deterministic repo assignment by hash."""
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def build_manifest(repo: str, date_folder: str, out_dir: Path) -> Path:
    """
    List top-level folder for repo/date_folder and save manifest.
    Uses single non-recursive call to minimize API usage.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_repo = repo.replace("/", "_")
    manifest_path = out_dir / f"{safe_repo}_{date_folder}.json"

    print(f"Building manifest for {repo}/{date_folder}...")
    items = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = []
    for item in items:
        if item.type == "file":
            # CDN URL (no auth)
            cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}"
            files.append({
                "path": item.path,
                "cdn_url": cdn_url,
                "size": getattr(item, "size", None)
            })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_by": "file_manifest.py",
        "files": files
    }

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Saved {len(files)} files -> {manifest_path}")
    return manifest_path

def load_manifest(repo: str, date_folder: str, manifest_dir: Path) -> Dict:
    safe_repo = repo.replace("/", "_")
    manifest_path = Path(manifest_dir) / f"{safe_repo}_{date_folder}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found: {manifest_path}. Run build first."
        )
    with open(manifest_path) as f:
        return json.load(f)

def cdn_dataloader(manifest: Dict, max_files: Optional[int] = None):
    """Yield CDN URLs for training (zero HF API calls during training)."""
    files = manifest["files"]
    if max_files:
        files = files[:max_files]
    for f in files:
        yield f["cdn_url"]

# Optional: Lightning Studio helpers
try:
    from lightning import Studio, Teamspace, Machine

    def get_or_create_studio(name: str, machine: str = "L40S"):
        """Reuse running studio; create only if none running."""
        for s in Teamspace.studios:
            if s.name == name and s.status == "running":
                print(f"Reusing running studio: {name}")
                return s
        print(f"Creating studio: {name} on {machine}")
        return Studio(
            name=name,
            machine=Machine(machine),
            create_ok=True
        )

    def ensure_studio_running(studio):
        """Restart if idle-stopped."""
        if studio.status != "running":
            print(f"Studio {studio.name} not running (status={studio.status}). Restarting...")
            studio.start(machine=studio.machine)
        return studio
except ImportError:
    get_or_create_studio = None
    ensure_studio_running = None

# CLI
def main():
    parser = argparse.ArgumentParser(description="HF file manifest for CDN-only training")
    sub = parser.add_subars(dest="cmd", required=True)

    build = sub.add_parser("build")
    build.add_argument("--repo", required=True, help="HF dataset repo (e.g., org/data)")
    build.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    build.add_argument("--out-dir", default=MANIFEST_DIR, help="Output dir")

    train = sub.add_parser("train")
    train.add_argument("--repo", required=True)
    train.add_argument("--date", required=True)
    train.add_argument("--manifest-dir", default=MANIFEST_DIR)
    train.add_argument("--script", help="Training script to invoke (optional)")

    args = parser.parse_args()

    if args.cmd == "build":
        build_manifest(args.repo, args.date, Path(args.out_dir))
    elif args.cmd == "train":
        manifest = load_manifest(args.repo, args.date, Path(args.manifest_dir))
        print(f"Loaded {len(manifest['files'])} files from manifest.")
        print("CDN URLs ready for dataloader (zero auth calls during training).")
        if args.script:
            import subprocess
            env = os.environ.copy()
            env["VANGUARD_MANIFEST"] = json.dumps(manifest)
            subprocess.run([sys.executable, args.script], env=env)

if __name__ == "__main__":
    main()
```

### Minimal `train.py` stub (if none exists)

```python
# train.py (minimal example)
import os
import json
import torch
from torch.utils.data import IterableDataset, DataLoader
import requests

class CDNTextDataset(IterableDataset):
    def __init__(self, urls, max_lines=10_000):
        self.urls = urls
        self.max_lines = max_lines

    def __iter__(self):
        for url in self.urls:
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                # Example: assume one text sample per line
                for i, line in enumerate(resp.text.splitlines()):
                    if i >= self.max_lines:
                        break
                    yield {"text": line.strip()}
            except Exception as e:
                print(f"Failed {url}: {e}")

