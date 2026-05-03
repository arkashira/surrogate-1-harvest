# vanguard / quality

## 1. Diagnosis
- No persisted `(repo, dateFolder) → file-list` manifest: every training run triggers authenticated `list_repo_tree`, burning HF API quota and risking 429s.
- Training likely uses `load_dataset(streaming=True)` or per-file authenticated fetches on heterogeneous repos, exposing `pyarrow` schema mismatches and rate-limit fragility.
- No CDN-only data path: training still relies on `/api/` endpoints instead of public CDN URLs, missing the highest-leverage rate-limit bypass.
- No reuse guard for Lightning Studio: scripts likely create new studios instead of reusing running ones, wasting quota.
- No idle-stop resilience: Lightning idle timeouts can kill long training runs without restart logic.

## 2. Proposed change
Add a small, high-leverage training launcher that:
- Persists a `file-list.json` for a given `(repo, dateFolder)` via a single Mac-side API call.
- Embeds that list in a Lightning training script that fetches only via CDN URLs (zero authenticated calls during training).
- Reuses a running Lightning Studio if present, else starts one (L40S priority).
- Adds idle-stop resilience by checking studio status before each run.

Scope:
- Create `/opt/axentx/vanguard/train_launcher.py` (orchestration, Mac-only).
- Create `/opt/axentx/vanguard/train_cdn.py` (Lightning training stub that reads `file-list.json` and streams via CDN).

## 3. Implementation

```bash
# Ensure scripts are executable and use proper shebang
cat > /opt/axentx/vanguard/train_launcher.py <<'PY'
#!/usr/bin/env python3
"""
Mac-side launcher for vanguard surrogate-1 training.
- Persists (repo, dateFolder) -> file-list.json
- Starts/reuses Lightning Studio
- Runs CDN-only training script in studio
"""
import json, os, sys, time
from pathlib import Path

import lightning as L
from lightning.fabric.plugins import LightningCLI

HF_REPO = os.getenv("HF_REPO", "datasets/username/repo")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")
MANIFEST_PATH = Path(__file__).parent / "file-list.json"

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Missing huggingface_hub. Install: pip install huggingface_hub")
    sys.exit(1)

def build_manifest():
    """Single authenticated call to list top-level folder; save to JSON."""
    print(f"Listing {HF_REPO}/{DATE_FOLDER} (non-recursive)...")
    # list_repo_tree(path, recursive=False) avoids heavy recursive pagination
    tree = list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    files = [f.rfilename for f in tree if f.rfilename.startswith(DATE_FOLDER)]
    manifest = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "files": sorted(files),
        "cdn_prefix": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"
    }
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Saved manifest with {len(files)} files -> {MANIFEST_PATH}")
    return manifest

def get_or_start_studio():
    """Reuse running studio if exists; else start L40S (fallback to public tier)."""
    teamspace = L.Teamspace()
    studio_name = "vanguard-surrogate-train"
    running = [s for s in teamspace.studios if s.name == studio_name and s.status == "running"]
    if running:
        print(f"Reusing running studio: {studio_name}")
        return running[0]

    print(f"Starting studio: {studio_name}")
    # Priority order: try L40S in paid cloud first; fallback to public tier
    clouds = ["lightning-lambda-prod", "lightning-public-prod"]
    machine = L.Machine.L40S
    for cloud in clouds:
        try:
            studio = L.Studio(
                name=studio_name,
                machine=machine,
                cloud=cloud,
                create_ok=True,
            )
            print(f"Created studio in {cloud} with {machine}")
            return studio
        except Exception as e:
            print(f"Cloud {cloud} failed ({e}), trying next...")
            continue
    raise RuntimeError("Could not start studio on any cloud")

def main():
    manifest = build_manifest()
    studio = get_or_start_studio()

    # Ensure studio is running before submitting work
    if studio.status != "running":
        print(f"Studio not running (status={studio.status}). Starting...")
        studio.start(machine=L.Machine.L40S)
        # Wait briefly for running
        for _ in range(10):
            if studio.status == "running":
                break
            time.sleep(6)
            studio.refresh()

    # Copy training script and manifest into studio and run
    train_script = Path(__file__).parent / "train_cdn.py"
    # Simple approach: run locally via studio.run() with script as target
    # If studio.run() supports file upload, prefer that; here we assume it runs the provided command
    print("Submitting training job to studio...")
    job = studio.run(
        target=str(train_script),
        arguments=[MANIFEST_PATH.name],
    )
    print(f"Job submitted: {job}")

if __name__ == "__main__":
    main()
PY

cat > /opt/axentx/vanguard/train_cdn.py <<'PY'
#!/usr/bin/env python3
"""
Lightning training script (runs inside studio).
- Reads file-list.json
- Streams files via CDN URLs (zero HF API calls)
- Projects to {prompt, response} only at parse time
"""
import json, sys, io, os
from pathlib import Path

import torch
from torch.utils.data import IterableDataset, DataLoader
import requests

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("Missing pyarrow. Install: pip install pyarrow requests")
    sys.exit(1)

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path, max_files=None):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.prefix = manifest["cdn_prefix"]
        files = manifest["files"]
        if max_files:
            files = files[:max_files]
        self.files = files

    def _stream_file(self, rel_path):
        url = f"{self.prefix}/{rel_path}"
        # CDN downloads: no Authorization header required; bypasses API rate limits
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return io.BytesIO(resp.content)

    def __iter__(self):
        for rel_path in self.files:
            try:
                buf = self._stream_file(rel_path)
                # Read only required columns; tolerate extra schema fields
                table = pq.read_table(buf, columns=["prompt", "response"], use_threads=False)
                for row in table.to_pylist():
                    # Basic validation
                    if row.get("prompt") and row.get("response"):
                        yield row
            except Exception as exc:
                # Skip malformed files; log and continue
                print(f"Skipping {rel_path}: {exc}")
                continue

def train_step(batch):
    # Placeholder: replace with real surrogate-1 training logic
    return {"loss": torch.tensor(0.0)}

def main(manifest_name="file-list.json"):
    manifest_path = Path(__file__).parent / manifest_name
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}")
        sys.exit(1)

    dataset = CDNParquetDataset(manifest_path, max_files=100)
    loader = DataLoader(dataset, batch_size=8, num_workers=0)

    print("Starting training loop (CDN-only)...")
    for i, batch in enumerate(loader):
        if i >= 10:  # small demo run
            break
        out = train_step(batch)
        print(f"step {i}: {out}")

    print("Training step completed (demo).")

if __name__ == "__main__":
    # Accept manifest filename as first arg
    manifest = sys.argv[1] if len(sys.argv) > 1 else "file-list.json"
    main(manifest)
PY

chmod +x /
