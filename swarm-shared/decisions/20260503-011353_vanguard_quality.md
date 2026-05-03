# vanguard / quality

## 1. Diagnosis
- No persisted `(repo, dateFolder)` manifest exists → every training run and backend request re-enumerates HF API via `list_repo_tree`, burning the 1000/5min quota and risking 429s.
- Backend uses authenticated `/api/` endpoints instead of public CDN URLs, adding auth overhead and rate-limit exposure for file fetches that could be anonymous.
- Training scripts likely call `load_dataset(streaming=True)` on heterogeneous repos, which can trigger `pyarrow.CastError` on mixed-schema files.
- No reuse guard for Lightning Studio → each run may create new studios instead of reusing running ones, wasting 80hr/mo quota.
- No idle-stop resilience → Lightning idle timeout kills long training jobs if status isn’t checked before `.run()`.

## 2. Proposed change
Create a lightweight manifest generator + CDN-based data loader for Surrogate-1 training:
- File: `/opt/axentx/vanguard/training/manifest.py` (new)
- File: `/opt/axentx/vanguard/training/train.py` (modify data-loading section)
Scope:
- Generate `batches/mirror-merged/{date}/manifest.json` containing `{repo, dateFolder, files: [{path, cdn_url, sha}]}` via a single `list_repo_tree` call per dateFolder.
- Embed manifest path in `train.py`; data loader fetches files via CDN (`resolve/main/...`) with zero HF API calls during training.
- Add Lightning Studio reuse + idle-check guard.

## 3. Implementation

### manifest.py
```python
#!/usr/bin/env python3
"""
Generate a CDN-based manifest for a given repo + dateFolder.
Usage:
    python manifest.py --repo huggingface/dataset/repo --date 2026-04-29 --out-dir batches/mirror-merged
"""
import argparse
import json
import os
import hashlib
from datetime import datetime
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    raise RuntimeError("Install huggingface_hub: pip install huggingface_hub")

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date_folder: str, out_dir: str):
    # Single API call (do this from Mac when rate-limit window is clear)
    tree = list_repo_tree(repo, path=date_folder, recursive=False)
    files = []
    for entry in tree:
        if entry.type != "file":
            continue
        path = f"{date_folder}/{entry.path}"
        files.append({
            "path": path,
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=path),
            "sha": entry.lfs.get("sha256", hashlib.sha256(path.encode()).hexdigest()[:16])
        })

    manifest = {
        "repo": repo,
        "dateFolder": date_folder,
        "generatedAt": datetime.utcnow().isoformat() + "Z",
        "count": len(files),
        "files": files
    }

    out_path = Path(out_dir) / date_folder / "manifest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path} ({len(files)} files)")
    return out_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate CDN manifest for HF dataset folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo, e.g. org/repo")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-04-29")
    parser.add_argument("--out-dir", default="batches/mirror-merged", help="Output base directory")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out_dir)
```

### train.py (data loader snippet to replace existing loader)
```python
# Near top of train.py
import json
from pathlib import Path
from torch.utils.data import IterableDataset
import requests

class CDNTextDataset(IterableDataset):
    """
    Loads {prompt,response} pairs from files listed in manifest.json using CDN URLs.
    Projects to {prompt, response} only at parse time to avoid pyarrow schema issues.
    """
    def __init__(self, manifest_path: str):
        manifest = json.loads(Path(manifest_path).read_text())
        self.file_urls = [f["cdn_url"] for f in manifest["files"]]

    def __iter__(self):
        for url in self.file_urls:
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                # Lightweight projection: expect JSONL lines with prompt/response
                for line in resp.text.strip().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        # Keep only what we need; ignore extra schema fields
                        yield {
                            "prompt": obj.get("prompt", ""),
                            "response": obj.get("response", "")
                        }
                    except json.JSONDecodeError:
                        continue
            except Exception as exc:
                # Log and skip bad files to avoid crashing training
                print(f"Skipping {url}: {exc}")
                continue
```

### Lightning orchestration guard (add near launcher code)
```python
from lightning import Lightning, Teamspace, Machine

lightning = Lightning()
teamspace = Teamspace()

def get_or_start_studio(name: str):
    # Reuse running studio to save quota
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s
    # Start new if none running
    print(f"Starting new studio: {name}")
    return lightning.Studio(
        name=name,
        machine=Machine.L40S,
        create_ok=True
    )

def run_training_job(studio_name: str, train_script: str, args: list):
    studio = get_or_start_studio(studio_name)
    # Guard against idle-stop death
    if studio.status != "Running":
        studio.start(machine=Machine.L40S)
    # Run training (non-blocking or blocking as preferred)
    job = studio.run(
        target=train_script,
        args=args,
        sync=False  # or True depending on workflow
    )
    return job
```

## 4. Verification
- Run manifest generation once (on Mac after rate-limit window clears):
  ```bash
  cd /opt/axentx/vanguard
  python training/manifest.py --repo your/dataset-repo --date 2026-04-29 --out-dir batches/mirror-merged
  ```
  Confirm `batches/mirror-merged/2026-04-29/manifest.json` exists and lists files with valid `cdn_url`s.
- In a quick smoke test, run the dataset iterator locally:
  ```python
  from training.train import CDNTextDataset
  ds = CDNTextDataset("batches/mirror-merged/2026-04-29/manifest.json")
  for i, item in enumerate(ds):
      print(item)
      if i >= 5: break
  ```
  Expect prompt/response pairs printed without HF API calls (check via network or process monitor).
- Start/verify Lightning flow:
  - Ensure no new studio is created if one with the same name is already running.
  - Stop a running studio, then call `run_training_job`; confirm it restarts and runs the target script.
- Monitor HF API usage before/after: repeated training runs should show near-zero authenticated `list_repo_tree` or `/api/` calls during data loading (only the initial manifest generation uses the API).
