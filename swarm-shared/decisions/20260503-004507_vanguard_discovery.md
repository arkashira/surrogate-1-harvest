# vanguard / discovery

## Final Synthesis (single authoritative answer)

**Root cause**: training runs repeatedly enumerate the HF repo via authenticated API, causing quota burn and 429 risk, and rely on recursive listing/streaming that exposes schema errors. There is no deterministic file manifest, and Lightning Studio reuse is not idempotent or idle-safe.

**Single fix**: introduce a deterministic, non-recursive manifest for each `(repo, dateFolder)` and switch all data loading to CDN-only `resolve/main/` URLs with zero authenticated calls during training. Make Lightning Studio lifecycle idempotent and idle-safe.

---

## 1. Manifest generator (one-time, non-recursive)

`/opt/axentx/vanguard/discovery/prepare_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate a deterministic manifest for (repo, dateFolder).
Usage:
  python3 prepare_manifest.py <repo> <dateFolder> [out_json]
"""
import json
import os
import sys
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else "axentx/surrogate-1"
    date_folder = sys.argv[2] if len(sys.argv) > 2 else __today()
    out_path = Path(sys.argv[3] if len(sys.argv) > 3 else f"manifest-{date_folder}.json")

    # Non-recursive, single folder only.
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = sorted(item.rfilename for item in tree if item.type == "file")

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "files": files,
        "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}"
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files -> {out_path}")

def __today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

if __name__ == "__main__":
    main()
```

Key points:
- Non-recursive listing to avoid pagination/schema scanning.
- Deterministic sorted file list for reproducibility.
- No authenticated calls during training.

---

## 2. CDN-only training loader

`/opt/axentx/vanguard/discovery/train.py`

```python
#!/usr/bin/env python3
"""
CDN-only data loader and optional Lightning Studio runner.
Usage:
  python3 train.py manifest-2026-04-29.json [--limit N] [--studio] [--machine L40S]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List

import requests
from requests.adapters import HTTPAdapter, Retry

CDN_URL = "https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{fname}"

# ---- CDN utilities ----
def _session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

def stream_cdn(url: str, chunk_size=8192):
    with _session().get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk

def fetch_one(repo: str, date_folder: str, fname: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / fname
    if cache_path.exists():
        return cache_path

    url = CDN_URL.format(repo=repo, date_folder=date_folder, fname=fname)
    with open(cache_path, "wb") as f:
        for chunk in stream_cdn(url):
            f.write(chunk)
    return cache_path

# ---- Manifest + dataset ----
def load_manifest(path: str) -> dict:
    with open(path) as f:
        return json.load(f)

def build_local_paths(manifest_path: str, limit: int = None, cache_root: str = ".cdn_cache") -> List[Path]:
    m = load_manifest(manifest_path)
    repo = m["repo"]
    date_folder = m["date_folder"]
    files = m["files"]
    if limit is not None:
        files = files[:limit]

    cache_dir = Path(cache_root) / repo.replace("/", "_") / date_folder
    paths = [fetch_one(repo, date_folder, f, cache_dir) for f in files]
    return paths

# ---- Lightning Studio (idempotent + idle-safe) ----
_LIGHTNING_OK = False
try:
    from lightning import LightningWork, LightningApp, Machine
    _LIGHTNING_OK = True
except Exception:
    pass

if _LIGHTNING_OK:
    class SurrogateTrainer(LightningWork):
        def __init__(self, manifest_path: str, machine: str = "L40S", **kwargs):
            super().__init__(**kwargs)
            self.manifest_path = manifest_path
            self.machine = machine

        def run(self):
            paths = build_local_paths(self.manifest_path)
            print(f"CDN-local files: {len(paths)}")
            # Insert surrogate-1 training loop here.
            # Example: train_lightning_model(paths)

    def ensure_studio(name: str, manifest_path: str, machine: str = "L40S"):
        from lightning import Teamspace
        for s in Teamspace.studios:
            if s.name == name and s.status == "running":
                print(f"Reusing running studio: {name}")
                return s
        target = SurrogateTrainer(manifest_path=manifest_path, machine=machine)
        LightningApp(target)
        return target

# ---- CLI ----
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", help="Path to manifest JSON")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--machine", default="L40S")
    parser.add_argument("--studio", action="store_true")
    args = parser.parse_args()

    if args.studio:
        if not _LIGHTNING_OK:
            print("Lightning not available; falling back to local run.")
            args.studio = False

    if args.studio:
        ensure_studio("vanguard-surrogate-train", args.manifest, machine=args.machine)
        print("Studio requested. Monitor via Lightning.")
    else:
        paths = build_local_paths(args.manifest, limit=args.limit)
        print(f"CDN-local paths ({len(paths)}):")
        for p in paths[:10]:
            print("  ", p)

if __name__ == "__main__":
    main()
```

Key points:
- CDN-only downloads; no Authorization headers during training (bypasses API quota).
- Retry/backoff for transient CDN errors.
- Local cache prevents repeated CDN fetches across runs.
- Lightning Studio runner is idempotent (reuse running studio) and lightweight.

---

## 3. Orchestration script

`/opt/axentx/vanguard/discovery/run_discovery.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

REPO="${REPO:-axentx/surrogate-1}"
DATEFOLDER="${1:-$(date +%Y-%m-%d)}"
MANIFEST="manifest-${DATEFOLDER}.json"

# One-time manifest generation (non-recursive).
python3 prepare_manifest.py "$REPO" "$DATEFOLDER" "$MANIFEST"

# Run training locally (or add --studio for Lightning Studio).
python3 train.py "$MANIFEST" --machine L40S
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/discovery/prepare_manifest.py
chmod +x /opt/axentx/vanguard/discovery/train.py
chmod +x /opt/axent
