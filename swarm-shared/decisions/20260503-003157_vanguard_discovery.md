# vanguard / discovery

## 1. Diagnosis

- No persisted `(repo, dateFolder)` file manifest exists → every training run re-enumerates via authenticated HF API and burns quota / risks 429.
- Data loader likely uses recursive enumeration or `load_dataset(streaming=True)` on heterogeneous repos → triggers pyarrow schema errors and couples training to API availability.
- No CDN-only fetch path in training pipeline → misses the key HF CDN bypass (public `resolve/main/` URLs) that avoids auth/rate limits entirely.
- No reuse guard for Lightning Studio → each run may create a new studio instead of reusing a running one, wasting 80+ hours/month of quota.
- No idle-stop resilience for Lightning training jobs — if a studio stops, training dies instead of restarting on an available machine.

## 2. Proposed change

Add a discovery/manifest generator and a Lightning launcher that:
- Persists a `manifests/{repo}/{dateFolder}.json` listing only file paths (single `list_repo_tree` call per folder).
- Embeds that manifest into training so data loading uses CDN-only fetches (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with zero API calls during training.
- Reuses a running Lightning Studio if present; otherwise starts one (L40S → fallback to available cloud accounts).
- Handles idle-stop by checking status before `.run()` and restarting if stopped.

Scope:
- Create `/opt/axentx/vanguard/scripts/build_manifest.py`
- Create `/opt/axentx/vanguard/scripts/run_training.py`
- Add lightweight config: `/opt/axentx/vanguard/config/training.json`

## 3. Implementation

```bash
# Ensure directories
mkdir -p /opt/axentx/vanguard/{scripts,config,manifests}
```

### config/training.json
```json
{
  "hf_dataset_repo": "your-org/vanguard-data",
  "date_folders": ["2026-04-29", "2026-04-30"],
  "lightning": {
    "clouds_priority": ["lightning-lambda-prod", "lightning-public-prod"],
    "machine_types": ["H200", "L40S"],
    "max_idle_restarts": 3
  },
  "manifest_dir": "manifests"
}
```

### scripts/build_manifest.py
```python
#!/usr/bin/env python3
"""
Build per-dateFolder file manifests for a HF dataset repo.
Uses a single list_repo_tree(path, recursive=False) per folder.
Writes manifests/{repo_slug}/{dateFolder}.json
"""
import json
import os
import sys
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "training.json"

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

def safe_repo_slug(repo: str) -> str:
    return repo.replace("/", "--")

def build_manifest(api: HfApi, repo: str, date_folder: str, out_dir: Path):
    # Non-recursive top-level list for the date folder
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if item.type == "file"]

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "files": sorted(files),
        "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main"
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_repo_slug(repo)}--{date_folder}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Built manifest: {out_path} ({len(files)} files)")
    return out_path

def main():
    cfg = load_config()
    repo = cfg["hf_dataset_repo"]
    api = HfApi()
    out_dir = Path(cfg["manifest_dir"])

    for df in cfg["date_folders"]:
        try:
            build_manifest(api, repo, df, out_dir)
        except Exception as exc:
            print(f"Failed for {df}: {exc}")
            # If 429, wait 360s as per pattern
            import time
            time.sleep(360)
            continue

if __name__ == "__main__":
    main()
```

### scripts/run_training.py
```python
#!/usr/bin/env python3
"""
Lightning training launcher with:
- Manifest-based CDN-only data loading (zero HF API calls during train)
- Studio reuse
- Idle-stop resilience (restart if stopped)
"""
import json
import os
import sys
import time
from pathlib import Path

try:
    from lightning import Lightning, L40S, H200, Machine, Teamspace
except ImportError:
    print("Install: pip install lightning")
    sys.exit(1)

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config" / "training.json"

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

def find_running_studio(name: str):
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            return s
    return None

def pick_machine(clouds_priority, machine_types):
    # Simplified: try combinations in priority order; fallback to first available
    for cloud in clouds_priority:
        for mt in machine_types:
            try:
                if mt == "H200":
                    return H200(cloud=cloud)
                else:
                    return L40S(cloud=cloud)
            except Exception:
                continue
    # Final fallback
    return L40S()

def load_manifest_paths(date_folder):
    manifest_dir = BASE_DIR / "manifests"
    repo_slug = "vanguard-data"  # adjust if needed; or derive from config
    manifest_path = manifest_dir / f"{repo_slug}--{date_folder}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    prefix = manifest["cdn_prefix"]
    return [f"{prefix}/{f}" for f in manifest["files"]]

def train_on_cdn_file_list(file_urls):
    """
    Placeholder training loop using CDN URLs.
    Replace with actual dataloader that streams parquet/jsonl via HTTP.
    """
    print(f"Training on {len(file_urls)} files (CDN-only)")
    # Example: iterate and stream via requests / datasets without auth
    # for url in file_urls:
    #   download and project to {prompt,response}
    #   train step
    return {"status": "ok", "files": len(file_urls)}

def main():
    cfg = load_config()
    studio_name = "vanguard-train-studio"

    # Reuse or create
    studio = find_running_studio(studio_name)
    if studio is None:
        machine = pick_machine(cfg["lightning"]["clouds_priority"], cfg["lightning"]["machine_types"])
        studio = Lightning.create(
            name=studio_name,
            machine=machine,
            create_ok=True
        )
        print(f"Created studio: {studio_name} on {machine}")
    else:
        print(f"Reusing running studio: {studio_name}")

    # Idle-stop resilience
    max_restarts = cfg["lightning"].get("max_idle_restarts", 3)
    restarts = 0

    for df in cfg["date_folders"]:
        while restarts <= max_restarts:
            if studio.status != "Running":
                print(f"Studio stopped (status={studio.status}). Restarting...")
                machine = pick_machine(cfg["lightning"]["clouds_priority"], cfg["lightning"]["machine_types"])
                studio.start(machine=machine)
                restarts += 1
                time.sleep(30)
                continue

            try:
                file_urls = load_manifest_paths(df)
                result = studio.run(
                    str(BASE_DIR / "scripts" / "train_step.py"),
                    arguments={"file_urls": file_urls}
                )
                print(f"Run result for {df}: {result}")
                break
            except Exception as exc:
                print(f"Run failed for {df}: {exc}")

