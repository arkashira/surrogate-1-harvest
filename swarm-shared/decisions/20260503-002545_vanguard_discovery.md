# vanguard / discovery

## Final Synthesized Answer

### 1. Diagnosis (Consolidated)
- **No persisted `(repo, dateFolder)` manifest** exists → every training run re-enumerates via authenticated HF API → quota burn + 429 risk.
- **Data loading uses authenticated `list_repo_tree`/`load_dataset` during training** instead of CDN-only fetches → wastes API quota on files that could bypass auth.
- **Training script likely re-creates Lightning Studio instead of reusing running ones** → burns quota on idle spin-up.
- **No guard against Lightning idle stop killing long-running training jobs** → training silently dies on timeout.
- **Mac/local runs may attempt `model.from_pretrained()` or heavy compute instead of delegating** → slow, non-portable discovery loop.

### 2. Proposed Change (Actionable)
Create `/opt/axentx/vanguard/scripts/discovery/make_manifest.py` and update the training launcher to:
- Run a single authenticated `list_repo_tree` (per dateFolder) from Mac, save `manifest-{repo}-{date}.json` to `vanguard/manifests/`.
- Embed that manifest in the Lightning training script so data loader uses only CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) with zero API calls during training.
- Reuse an existing running Lightning Studio if present; otherwise start one (L40S priority, fallback to public tier).
- Add idle-check + restart guard before each `.run()` to survive studio stop/timeout.
- Add local fallback mode so Mac can run discovery without Lightning when needed.

Scope: new file + small edits to launcher/train script under `vanguard/scripts/discovery/` and `vanguard/train/`.

### 3. Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/{manifests,scripts/discovery,train}
cd /opt/axentx/vanguard
```

#### manifest generator (run from Mac)
`scripts/discovery/make_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate a CDN-only manifest for a dataset repo/dateFolder.
Run from Mac (or any machine) while HF API window is clear.
Outputs: manifests/manifest-{repo_slug}-{date}.json
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("pip install huggingface_hub")
    sys.exit(1)

HF_API = HfApi()

def list_date_files(repo_id: str, date_folder: str):
    """
    Non-recursive top-level list for date_folder.
    Returns list of dict: { "path": ..., "cdn_url": ... }
    """
    try:
        files = HF_API.list_repo_tree(repo_id, path=date_folder, recursive=False)
    except Exception as e:
        print(f"HF API error: {e}", file=sys.stderr)
        return []

    out = []
    for f in files:
        if f.get("type") != "file":
            continue
        path = f["path"]
        cdn = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
        out.append({"path": path, "cdn_url": cdn, "size": f.get("size", 0)})
    return out

def main():
    parser = argparse.ArgumentParser(description="Create CDN manifest for training")
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/ds)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out-dir", default="manifests", help="Output directory")
    args = parser.parse_args()

    print(f"Listing {args.repo}/{args.date} ...")
    items = list_date_files(args.repo, args.date)
    if not items:
        print("No files found or API error.", file=sys.stderr)
        sys.exit(1)

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": items,
        "total_files": len(items),
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = args.repo.replace("/", "-")
    out_path = out_dir / f"manifest-{slug}-{args.date}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x scripts/discovery/make_manifest.py
```

#### Lightning launcher with reuse + idle guard + local fallback
`scripts/discovery/run_training.py`
```python
#!/usr/bin/env python3
"""
Launch surrogate-1 training on Lightning with:
- manifest-driven CDN-only data loading
- studio reuse
- idle-stop guard + restart
- local fallback mode (no Lightning required)
"""
import json
import argparse
import sys
import subprocess
from pathlib import Path

MANIFEST_DIR = Path(__file__).parent.parent / "manifests"
TRAIN_SCRIPT = Path(__file__).parent.parent / "train" / "train.py"

def find_running_studio(name_prefix="vanguard-train"):
    try:
        from lightning.app import LightningStudio
        studios = LightningStudio.list()
        for s in studios:
            if s.name.startswith(name_prefix) and s.status == "Running":
                return s
    except Exception:
        pass
    return None

def run_local(manifest_path, output_dir):
    """Local fallback: run training script directly with manifest."""
    cmd = [
        sys.executable, str(TRAIN_SCRIPT),
        "--manifest", str(manifest_path),
        "--output-dir", str(output_dir.absolute()),
    ]
    print(f"Running locally: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Training failed with code {proc.returncode}")
    return proc.returncode

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--machine", default="lightning-public-prod")
    parser.add_argument("--name", default="vanguard-train")
    parser.add_argument("--local", action="store_true", help="Run locally without Lightning")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    if args.local:
        print("Running in local mode (no Lightning).")
        sys.exit(run_local(manifest_path, Path("outputs")))

    # Lightning mode
    try:
        from lightning import LightningWork, LightningApp, Machine
    except ImportError:
        print("Lightning not available. Use --local or install lightning.")
        sys.exit(1)

    class SurrogateTrainWork(LightningWork):
        def __init__(self, manifest_path, machine="lightning-public-prod", **kwargs):
            super().__init__(**kwargs)
            self.manifest_path = str(manifest_path)
            self.machine = machine
            self._studio = None

        def run(self):
            # Load manifest (CDN-only file list)
            manifest = json.loads(Path(self.manifest_path).read_text())
            print(f"Loaded manifest: {manifest['repo']} {manifest['date']} ({manifest['total_files']} files)")

            # Build data loader that uses only CDN URLs (no HF API calls)
            cmd = [
                sys.executable, str(TRAIN_SCRIPT),
                "--manifest", self.manifest_path,
                "--output-dir", str(Path("outputs").absolute()),
            ]

            # If studio stopped by idle timeout, restart it
            if self._studio and self._studio.status != "Running":
                print("Studio not running (idle stop?). Restarting...")
                self._studio.start(machine=Machine(self.machine))

            # Execute training script inside studio environment
            proc = subprocess.run(cmd, capture_output=False)
            if proc.returncode != 0:
                raise RuntimeError(f"Training failed with code {proc.returncode}")

    # Reuse running studio if available
    studio = find_running_studio(args.name)
    if studio:
        print(f"Reusing running studio: {
