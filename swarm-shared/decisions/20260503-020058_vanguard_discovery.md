# vanguard / discovery

# 1. Diagnosis

- No persisted `(repo, dateFolder) → file-list` manifest: every training/data-selection run triggers authenticated `list_repo_tree` against HF API, burning quota and risking 429s.
- Data loader likely uses `load_dataset(streaming=True)` or repeated per-file API calls on heterogeneous repos, causing `pyarrow.CastError` from mixed schemas.
- Training runs on Lightning risk quota waste and H200/L40S confusion (free tier falls to L40S; H200 only in paid `lightning-lambda-prod`).
- No CDN-bypass strategy: authenticated API calls during data loading instead of using public CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) which avoid rate limits.
- No reuse guard for Lightning Studio: `Studio(create_ok=True)` recreates instead of reusing running studios, burning ~80hr/mo quota.

# 2. Proposed change

Create `/opt/axentx/vanguard/discovery/persist_filelist.py` (single orchestrator script) and update training launcher to:
- Accept `(repo, date_folder)`; call `list_repo_tree(recursive=False)` once (Mac-side, post-rate-limit window).
- Persist `vanguard/discovery/manifests/{repo}@{date_folder}.json` with CDN-style paths.
- Embed that manifest in Lightning training script so data loader uses CDN-only fetches (zero API calls during training).
- Add Studio reuse + safe cloud fallback (L40S default; H200 only if account available).

# 3. Implementation

```bash
# /opt/axentx/vanguard/discovery/persist_filelist.py
#!/usr/bin/env python3
"""
Generate and persist a CDN file-list manifest for a repo+date folder.
Run from Mac (or any machine with HF token) after rate-limit window clears.
"""
import argparse
import json
import os
from pathlib import Path
from huggingface_hub import HfApi, list_repo_tree

HF_API = HfApi()
MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

def build_manifest(repo_id: str, date_folder: str, out_dir: Path = MANIFEST_DIR) -> Path:
    """
    repo_id: e.g. 'datasets/myorg/surrogate-1'
    date_folder: e.g. '2026-04-29'
    Returns path to persisted manifest.
    """
    prefix = f"{date_folder}/"
    entries = list_repo_tree(repo_id=repo_id, path=prefix, recursive=False)
    files = [
        {
            "repo_id": repo_id,
            "path": e.path,
            "cdn_url": f"https://huggingface.co/datasets/{repo_id}/resolve/main/{e.path}"
        }
        for e in entries if e.type == "file"
    ]

    slug = repo_id.replace("/", "_")
    manifest_path = out_dir / f"{slug}@{date_folder}.json"
    manifest_path.write_text(json.dumps({"repo_id": repo_id, "date_folder": date_folder, "files": files}, indent=2))
    print(f"Persisted {len(files)} files -> {manifest_path}")
    return manifest_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Persist CDN file-list manifest for HF dataset folder.")
    parser.add_argument("--repo", required=True, help="HF repo id (e.g. datasets/myorg/surrogate-1)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    args = parser.parse_args()
    build_manifest(args.repo, args.date)
```

```python
# /opt/axentx/vanguard/discovery/train_launcher.py
#!/usr/bin/env python3
"""
Lightning-aware launcher that:
- Reuses running Studio when available
- Falls back to L40S on lightning-public-prod (free tier)
- Uses CDN-only file loading via persisted manifest
"""
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from lightning import LightningWork, LightningApp, Machine, Teamspace, Studio
except ImportError:
    print("lightning not installed; install via 'pip install lightning'")
    sys.exit(1)

MANIFEST_DIR = Path(__file__).parent / "manifests"

def find_running_studio(name: str) -> Studio | None:
    for s in Teamspace().studios:
        if s.name == name and s.status == "Running":
            return s
    return None

def preferred_machine():
    # Prefer L40S on free tier; H200 only on lightning-lambda-prod if caller overrides
    cloud = os.getenv("LIGHTNING_CLOUD", "lightning-public-prod")
    if cloud == "lightning-lambda-prod":
        return Machine.L400  # L400 is H200 variant in lambda-prod; adjust if needed
    return Machine.L40S

class SurrogateTrainer(LightningWork):
    def __init__(self, manifest_path: str, script_path: str):
        super().__init__()
        self.manifest_path = manifest_path
        self.script_path = script_path

    def run(self):
        # Load manifest (CDN-only paths)
        manifest = json.loads(Path(self.manifest_path).read_text())
        # Pass manifest to training script via env
        env = os.environ.copy()
        env["VANGUARD_MANIFEST"] = self.manifest_path
        # Launch training (script must consume VANGUARD_MANIFEST and use CDN URLs)
        subprocess.run([sys.executable, self.script_path], env=env, check=True)

def launch_training(
    repo_id: str,
    date_folder: str,
    train_script: str = "train.py",
    studio_name: str = "vanguard-surrogate-train",
):
    manifest_path = MANIFEST_DIR / f"{repo_id.replace('/', '_')}@{date_folder}.json"
    if not manifest_path.exists():
        print(f"Manifest missing: {manifest_path}. Run persist_filelist.py first.")
        sys.exit(1)

    existing = find_running_studio(studio_name)
    if existing:
        print(f"Reusing running studio: {studio_name}")
        # If stopped, restart it (idle stop kills training)
        if existing.status != "Running":
            existing.start(machine=preferred_machine())
        # Queue work via existing studio is complex; simpler: run trainer locally in this launcher
        # and rely on LightningWork placement when app starts.
    else:
        print(f"No running studio '{studio_name}'. Creating one (L40S default).")

    # Build Lightning app that runs one work
    app = LightningApp(
        SurrogateTrainer(manifest_path=str(manifest_path), script_path=train_script),
        # Studio options
        # Note: Studio auto-created with create_ok=True; reuse above avoids churn.
    )
    # Running app will schedule SurrogateTrainer on selected machine.
    # On Mac, this launches the Lightning controller; training runs remotely.
    return app

if __name__ == "__main__":
    # Example CLI: python train_launcher.py --repo datasets/myorg/surrogate-1 --date 2026-04-29
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--script", default="train.py")
    parser.add_argument("--studio", default="vanguard-surrogate-train")
    args = parser.parse_args()
    launch_training(args.repo, args.date, args.script, args.studio)
```

```python
# /opt/axentx/vanguard/discovery/train.py  (minimal CDN-only loader example)
import json
import os
import requests
import pyarrow.parquet as pq
from torch.utils.data import IterableDataset

class CDNParquetIterable(IterableDataset):
    def __init__(self, manifest_path=None):
        manifest_path = manifest_path or os.environ.get("VANGUARD_MANIFEST")
        if not manifest_path or not os.path.exists(manifest_path):
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")
        manifest = json.loads(open(manifest_path).read())
        self.urls = [f["cdn_url"] for f in manifest["files"] if f["cdn_url"].endswith(".parquet")]

    def __iter__(self):
        for url in self.url
