# vanguard / discovery

## Final Synthesized Implementation

Below is the single, authoritative version that merges the strongest, non-contradictory parts of both proposals and resolves all conflicts in favor of **correctness + concrete actionability**.

### 1) Diagnosis (merged and resolved)
- **No durable ingestion manifest** → every run re-lists HF repos via API (paginated) → guaranteed 429s and quota burn.  
  **Fix**: List once, write `manifest.json`, reuse across runs.
- **Training uses `load_dataset`/`list_repo_files`** → hits auth-required API limits during training.  
  **Fix**: CDN-only fetches via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth).
- **No reuse guard for Lightning Studio** → idle-stop kills training and wastes quota by recreating.  
  **Fix**: Detect running studio by name; reuse if status is `Running`; else create.
- **No pre-listed file manifest embedded in training** → each epoch can trigger API calls instead of pure CDN fetches.  
  **Fix**: Pass `--manifest` to training; training loads file list from JSON and never calls HF listing APIs.
- **Missing orchestration script with proper Bash shebang/execution pattern** → wrapper exec errors and cron failures.  
  **Fix**: Provide executable `.sh` with `#!/usr/bin/env bash`, `set -euo pipefail`, and explicit `cd`.
- **Missing deterministic repo selection / commit cap handling** → ingestion likely blocked by HF rate limits.  
  **Fix**: Use repo tree listing (non-recursive per folder) to minimize calls; cache manifest; avoid per-epoch listing.

### 2) Implementation

```bash
# Create orchestrator
cat > /opt/axentx/vanguard/orchestrate_discovery.py << 'PYEOF'
#!/usr/bin/env python3
"""
Orchestrate discovery pipeline:
- List HF repo tree once -> manifest.json
- Reuse running Lightning Studio or create one
- Launch training with CDN-only data loading
"""
import json, os, sys
from pathlib import Path

HF_REPO = os.getenv("HF_REPO", "axentx/vanguard-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-05-02")
MANIFEST_PATH = Path("/opt/axentx/vanguard/manifest.json")
TRAIN_SCRIPT = Path("/opt/axentx/vanguard/train_discovery.py")

try:
    from lightning_sdk import Studio, Machine, Teamspace
except ImportError:
    print("lightning-sdk not installed; install via: pip install lightning")
    sys.exit(1)

def build_manifest():
    """List repo tree (non-recursive per folder) and save manifest."""
    try:
        from huggingface_hub import list_repo_tree
    except ImportError:
        print("huggingface_hub not installed; install via: pip install huggingface_hub")
        sys.exit(1)

    print(f"Listing HF repo tree: {HF_REPO}/{DATE_FOLDER}")
    items = list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_FOLDER,
        recursive=False
    )
    files = [f.rfilename for f in items if f.type == "file"]
    manifest = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "files": files
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {MANIFEST_PATH} ({len(files)} files)")
    return manifest

def reuse_or_create_studio():
    """Reuse running studio if exists; else create with L40S (free-tier fallback)."""
    teamspace = Teamspace()
    studio_name = "vanguard-discovery"
    running = [s for s in teamspace.studios if s.name == studio_name and s.status == "Running"]
    if running:
        studio = running[0]
        print(f"Reusing running studio: {studio.name} (id={studio.id})")
        return studio

    print(f"Creating studio: {studio_name}")
    # Try L40S first (free-tier compatible); fallback to available machine
    try:
        studio = Studio.create(
            name=studio_name,
            machine=Machine.L40S,
            cloud="lightning-public-prod"
        )
    except Exception as e:
        print(f"L40S unavailable ({e}); using default machine")
        studio = Studio.create(name=studio_name, create_ok=True)
    return studio

def main():
    manifest = build_manifest()
    studio = reuse_or_create_studio()

    # Ensure studio is running before run()
    if studio.status != "Running":
        print(f"Studio stopped (status={studio.status}); restarting...")
        studio.start(machine=Machine.L40S)

    cmd = [
        "python", str(TRAIN_SCRIPT),
        "--manifest", str(MANIFEST_PATH),
        "--output", "/teamspace/volumes/scratch/vanguard-output"
    ]
    print(f"Running training: {' '.join(cmd)}")
    run = studio.run(cmd, cwd="/opt/axentx/vanguard")
    print(f"Run submitted: {run.id}")

if __name__ == "__main__":
    main()
PYEOF

# Create training stub (CDN-only)
cat > /opt/axentx/vanguard/train_discovery.py << 'PYEOF'
#!/usr/bin/env python3
"""
Train using CDN-only fetches (no HF API auth).
Usage: python train_discovery.py --manifest manifest.json --output /path
"""
import argparse, json, requests, os, sys
from pathlib import Path

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    return p.parse_args()

def load_via_cdn(repo: str, path: str):
    """Download file via CDN (no auth)."""
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

def project_to_pair(content: bytes, filename: str):
    """
    Project raw file to {prompt, response}.
    Placeholder: assumes JSONL with 'prompt' and 'response' fields.
    Extend per actual schema.
    """
    try:
        data = json.loads(content.decode())
        if isinstance(data, dict) and "prompt" in data and "response" in data:
            return {"prompt": data["prompt"], "response": data["response"]}
    except Exception:
        pass

    # Fallback: return minimal metadata
    return {"prompt": "", "response": "", "_source_file": filename}

def main():
    args = parse_args()
    manifest = json.loads(args.manifest.read_text())
    repo = manifest["repo"]
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    records = []
    for fpath in manifest["files"]:
        try:
            content = load_via_cdn(repo, fpath)
            pair = project_to_pair(content, fpath)
            records.append(pair)
        except Exception as e:
            print(f"Failed {fpath}: {e}", file=sys.stderr)

    out_file = out_dir / "train_pairs.jsonl"
    with out_file.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    print(f"Wrote {len(records)} pairs to {out_file}")

if __name__ == "__main__":
    main()
PYEOF

# Create wrapper script with proper shebang
cat > /opt/axentx/vanguard/run_discovery.sh << 'SHEOF'
#!/usr/bin/env bash
# Wrapper for vanguard discovery pipeline.
# Set SHELL in crontab: SHELL=/bin/bash
set -euo pipefail

cd /opt/axentx/vanguard
exec python3 orchestrate_discovery.py
SHEOF

chmod +x /opt/axentx/vanguard/run_discovery.sh
chmod +x /opt/axentx/vanguard/orchestrate_discovery.py
chmod +x /opt/axentx/vanguard/train_discovery.py
```

### 3) Verification

```bash
# 1) Check files exist and are executable
ls -la /opt/axentx/vanguard/orchestrate_discovery.py /opt/axentx/vanguard/train_discovery.py /
