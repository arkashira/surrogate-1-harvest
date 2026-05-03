# vanguard / quality

## 1. Diagnosis
- No persisted `(repo, dateFolder)` manifest exists → every training run re-authenticates via `list_repo_tree` and burns 1000/5min HF API quota, causing intermittent 429s.
- Training script likely still uses `load_dataset(streaming=True)` or authenticated API paths instead of public CDN URLs, wasting rate-limit budget on downloads that could be unauthenticated CDN fetches.
- No guard to reuse an already-running Lightning Studio; script probably creates new studios each run and burns 80+ hours/month of quota on idle/startup overhead.
- Dataset ingestion probably writes mixed-schema files into `enriched/` with extra `source`/`ts` columns instead of projecting to `{prompt, response}` and using clean `batches/mirror-merged/{date}/{slug}.parquet` layout.
- No fallback for Lightning idle-stop: if a studio is stopped, training dies instead of restarting on `L40S` (or `H200` in `lightning-lambda-prod` when available).

## 2. Proposed change
File scope: `/opt/axentx/vanguard/train.py` (create or update) and `/opt/axentx/vanguard/manifest.json` (generated artifact).  
Scope: add a small orchestration wrapper that:
- lists target folder once (post-rate-limit window), writes `manifest.json`
- trains via Lightning Studio using **CDN-only** URLs (zero authenticated fetches during dataload)
- reuses a running studio if present
- projects datasets to `{prompt, response}` on-the-fly and streams from CDN via `datasets` with `streaming=True` + `use_auth_token=False`

## 3. Implementation
Create/update `train.py`:

```python
#!/usr/bin/env python3
"""
train.py
- Generates/reads (repo, dateFolder) manifest to avoid HF API list calls during training.
- Uses HF CDN URLs (unauthenticated) for data streaming.
- Reuses running Lightning Studio or starts one on L40S (fallback H200 on lightning-lambda-prod).
"""
import json, os, time, pathlib, subprocess, sys
from datetime import datetime, timezone

try:
    from lightning_sdk import Studio, Machine, Teamspace
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "lightning"])
    from lightning_sdk import Studio, Machine, Teamspace

HF_REPO = os.getenv("HF_REPO", "datasets/axentx/vanguard-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
MANIFEST_PATH = pathlib.Path(__file__).parent / "manifest.json"
OUTPUT_DIR = pathlib.Path(__file__).parent / "lightning_outputs"

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest() -> dict:
    """
    One-time authenticated list (run from Mac orchestration).
    Uses recursive=False per subfolder to minimize pagination.
    """
    try:
        from huggingface_hub import list_repo_tree
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "huggingface-hub"])
        from huggingface_hub import list_repo_tree

    root = f"{HF_REPO}/{DATE_FOLDER}"
    tree = list_repo_tree(root, recursive=False)
    files = []
    for entry in tree:
        if entry.rfilename.endswith(".parquet"):
            files.append({
                "repo": HF_REPO,
                "path": f"{DATE_FOLDER}/{entry.rfilename}",
                "cdn_url": HF_CDN_TEMPLATE.format(repo=HF_REPO, path=f"{DATE_FOLDER}/{entry.rfilename}")
            })

    manifest = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "files": files
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    return manifest

def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return build_manifest()

def project_to_prompt_response(batch):
    """Keep only {prompt, response}; drop everything else."""
    return {
        "prompt": batch["prompt"],
        "response": batch["response"]
    }

def train_on_lightning(manifest: dict):
    teamspace = Teamspace()
    studio_name = f"vanguard-train-{DATE_FOLDER.replace('-', '')}"
    studio = None
    for s in teamspace.studios:
        if s.name == studio_name and s.status == "Running":
            studio = s
            break

    machine = Machine.L40S
    # If you have access to lightning-lambda-prod and want H200:
    # machine = Machine.H200  # only in lightning-lambda-prod

    if studio is None:
        studio = teamspace.studios.create(
            name=studio_name,
            machine=machine,
            create_ok=True
        )
    else:
        print(f"Reusing running studio: {studio_name}")

    # Prepare train script that will run inside studio (CDN-only, zero HF API calls)
    train_script = """
import json, os
from datasets import load_dataset

HF_REPO = "{repo}"
DATE_FOLDER = "{date_folder}"
MANIFEST = {manifest_json}

def gen():
    for f in MANIFEST["files"]:
        ds = load_dataset(
            "parquet",
            data_files={"train": f["cdn_url"]},
            streaming=True,
            use_auth_token=False
        )
        for sample in ds["train"]:
            # project to {prompt, response}
            yield {
                "prompt": sample["prompt"],
                "response": sample["response"]
            }

if __name__ == "__main__":
    # Example: iterate and train (replace with your trainer)
    import torch
    from torch.utils.data import DataLoader

    ds = list(gen())  # small demo; in practice keep streaming
    print(f"Loaded {len(ds)} samples from CDN")
    # Your training loop here
"""

    script_path = pathlib.Path(__file__).parent / "train_in_studio.py"
    script_path.write_text(train_script.format(
        repo=manifest["repo"],
        date_folder=manifest["date_folder"],
        manifest_json=json.dumps(manifest["files"])
    ))

    # Run training in studio
    run = studio.run(
        target=str(script_path),
        requirements=["torch", "datasets", "pyarrow"],
        cwd=str(pathlib.Path(__file__).parent),
        name=f"vanguard-train-run-{int(time.time())}"
    )
    print(f"Started run: {run.name}")
    return run

def main():
    manifest = load_manifest()
    print(f"Manifest: {len(manifest['files'])} parquet files")
    if not manifest["files"]:
        print("No parquet files found; aborting.")
        return
    train_on_lightning(manifest)

if __name__ == "__main__":
    main()
```

Make executable (if invoked via CLI wrappers):

```bash
chmod +x /opt/axentx/vanguard/train.py
```

Wrapper best-practice (if used from cron/scripts): ensure `SHELL=/bin/bash` and invoke via `bash train.py "$@"` if wrapping in Bash.

## 4. Verification
1. Generate manifest once (after rate-limit window is clear):
   ```bash
   cd /opt/axentx/vanguard && python3 train.py
   ```
   Confirm `manifest.json` exists and lists parquet files with `cdn_url` fields.

2. Confirm CDN URLs are reachable without auth:
   ```bash
   curl -I "$(python3 -c "import json; m=json.load(open('manifest.json')); print(m['files'][0]['cdn_url'])")"
   ```
   Expect `200 OK` (no `401`/`403`).

3. Confirm no authenticated HF API calls during dataload:
   - Run the inner `train_in_studio.py` locally with `use_auth_token=False` and monitor traffic; all requests should target `https://huggingface.co/datasets/.../resolve/main/...` and return `200` without Authorization headers.

4. Confirm studio reuse:
   - Run `train.py` twice while the first studio is still running; second run should print
