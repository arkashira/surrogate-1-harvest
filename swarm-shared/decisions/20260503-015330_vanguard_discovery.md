# vanguard / discovery

# Final Synthesized Solution

## 1. Diagnosis (Consolidated)
- **No file-list cache**: Every run performs authenticated `list_repo_tree`, burning HF API quota and risking 429s.
- **No CDN-only data loading**: Training uses `load_dataset`/per-file API calls instead of `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.
- **No commit-cap mitigation**: No deterministic repo-sibling routing for HF’s 128 writes/hr/repo limit.
- **Lightning Studio waste**: Training recreates studios instead of reusing running ones; no idle-stop guard.
- **Missing launcher guardrails**: No check for existing studios or external stop state.

## 2. Proposed Change (Single Coherent Plan)
1. **Create `/opt/axentx/vanguard/ops/discovery/file_list_cache.py`**  
   - Single authenticated `list_repo_tree` per `(repo, dateFolder)` → write `file_list.json` to cache.
   - Deterministic sibling index via `sha256(repo) % N` for commit-cap spreading.
   - Emit CDN URLs for zero-auth training fetches.

2. **Update launcher (`/opt/axentx/vanguard/train/launcher.py`)**  
   - Load cached file list; fail-fast if missing (forces cache-first workflow).
   - Reuse running Lightning Studio by name; restart only if stopped.
   - Guard against external idle-stop by checking `studio.status`.

3. **Update training stub (`/opt/axentx/vanguard/train/train.py`)**  
   - Use CDN-only fetches via `requests` (no HF auth during training).
   - Accept file list as JSON argument; stream files with retries and timeouts.

## 3. Implementation (Resolved + Actionable)

```bash
# /opt/axentx/vanguard/ops/discovery/file_list_cache.py
#!/usr/bin/env python3
"""
Cache repo+dateFolder file lists to avoid HF API pagination/429.
Usage:
  python file_list_cache.py \
    --repo datasets/axentx/vanguard-mirror \
    --date 2026-05-03 \
    --out cache/file_list.json
"""
import argparse
import json
import hashlib
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    raise SystemExit("pip install huggingface_hub")

CACHE_DIR = Path(__file__).parent.parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True, parents=True)

def repo_sibling(repo: str, n_siblings: int = 5) -> int:
    """Deterministic sibling index for HF commit-cap spreading."""
    digest = hashlib.sha256(repo.encode()).digest()
    return int.from_bytes(digest, "big") % n_siblings

def build_file_list(repo: str, date_folder: str, out_path: Path) -> list[str]:
    """
    Single list_repo_tree call (non-recursive) for date_folder.
    Returns repo-relative paths.
    """
    print(f"Listing {repo}/{date_folder} ...")
    items = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [f.rfilename for f in items if f.type == "file"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(files, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")
    return files

def main() -> None:
    parser = argparse.ArgumentParser(description="Cache HF repo file list.")
    parser.add_argument("--repo", required=True, help="HF repo id (e.g. datasets/axentx/vanguard-mirror)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default=None, help="Output JSON path (default: cache/{repo_slug}/{date}/file_list.json)")
    args = parser.parse_args()

    slug = args.repo.replace("/", "_")
    out = Path(args.out) if args.out else CACHE_DIR / slug / args.date / "file_list.json"
    files = build_file_list(args.repo, args.date, out)

    # Print CDN URLs for embedding in training scripts (zero-auth fetch)
    for p in files[:3]:
        print(f"CDN: https://huggingface.co/datasets/{args.repo}/resolve/main/{args.date}/{p}")
    if len(files) > 3:
        print(f"... and {len(files)-3} more")

    # Print sibling index for commit-cap routing
    sib = repo_sibling(args.repo)
    print(f"Sibling index (for commit-cap spreading): {sib}")

if __name__ == "__main__":
    main()
```

```python
# /opt/axentx/vanguard/train/launcher.py
#!/usr/bin/env python3
"""
Lightning training launcher with:
- file-list cache usage (CDN-only data loading)
- Studio reuse + idle-stop guard
- deterministic HF sibling routing
"""
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from lightning import LightningWork, LightningApp, Studio
except ImportError:
    print("pip install lightning")
    sys.exit(1)

REPO = "datasets/axentx/vanguard-mirror"
DATE = os.getenv("VANGUARD_DATE", "2026-05-03")
CACHE_FILE = Path(__file__).parent.parent / "cache" / REPO.replace("/", "_") / DATE / "file_list.json"

def load_file_list() -> list[str]:
    if not CACHE_FILE.is_file():
        raise RuntimeError(
            f"File list cache missing: {CACHE_FILE}. "
            "Run file_list_cache.py before training."
        )
    with open(CACHE_FILE) as f:
        return json.load(f)

def pick_sibling_repo(sibling_index: int = 0) -> str:
    """Return sibling repo name for writes (spread HF commit cap)."""
    base = REPO.replace("datasets/", "").replace("/", "-")
    return f"datasets/{base}-s{sibling_index}"

class VanguardTrainer(LightningWork):
    def __init__(self, file_list, **kwargs):
        super().__init__(**kwargs)
        self.file_list = file_list
        self.studio = None

    def run(self):
        # Guard: if studio stopped externally, restart
        if self.studio is None or getattr(self.studio, "status", None) != "running":
            print("Starting/reusing Lightning Studio...")
            existing = [s for s in Studio.list() if s.name == "vanguard-train" and getattr(s, "status", None) == "running"]
            if existing:
                self.studio = existing[0]
                print(f"Reusing running studio: {self.studio.name}")
            else:
                self.studio = Studio(
                    name="vanguard-train",
                    create_ok=True,
                    machine="L40S",
                )
                print("Created new studio (L40S).")

        # Training script using CDN-only file list (zero auth during data load)
        cmd = [
            sys.executable,
            str(Path(__file__).parent / "train.py"),
            "--file-list",
            json.dumps(self.file_list),
            "--repo",
            REPO,
        ]
        subprocess.run(cmd, check=True)

def main():
    file_list = load_file_list()
    sib_idx = int(os.getenv("HF_SIBLING", repo_sibling(REPO)))
    sibling_repo = pick_sibling_repo(sib_idx)
    print(f"Using sibling repo for writes: {sibling_repo}")

    app = LightningApp(VanguardTrainer(file_list=file_list, cloud_compute="L40S"))

if __name__ == "__main__":
    main()
```

```python
# /opt/axentx/vanguard/train/train.py
#!/usr/bin/env python3
"""
Train using CDN-only file list (no HF auth during data loading).
Expects --file-list JSON string and --repo.
"""
import argparse
import json
import time
import requests
from tqdm import tqdm

def cdn_fetch(repo: str, path: str, retries: int = 3, timeout: int = 30):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    for
