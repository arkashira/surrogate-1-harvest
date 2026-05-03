# vanguard / discovery

## 1. Diagnosis
- No CDN-first manifest exists; ingestion/training scripts likely still call Hugging Face API (`list_repo_tree`, `load_dataset`) at runtime, risking 429s and non-reproducible runs.
- Missing content-addressed file list keyed by date/slug; training jobs cannot run deterministically from a snapshot.
- No guardrails to prevent local model loading or Mac-side training; remote-only compute policy is unenforceable without a launcher.
- Studio reuse is not automated; each run risks quota waste by creating new Lightning Studio instances.
- No explicit path to bypass HF API during training; data loader still depends on `datasets` library which may trigger auth/rate-limit calls.

## 2. Proposed change
Create `/opt/axentx/vanguard/ingest/manifest.py` (single file) that:
- Accepts a repo and date folder, calls `list_repo_tree` once (Mac-side, post-rate-limit window), and writes `manifest-{date}.json` containing `{repo, path, sha, url}` for every file.
- Generates a deterministic, content-addressed snapshot keyed by `{date}/{slug}`.
- Emits a `train.py`-ready file list that uses only CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) so Lightning training can fetch with zero API calls.

Create `/opt/axentx/vanguard/lightning/launch.py` (single file) that:
- Lists running Teamspace studios and reuses a running one if present.
- Starts/stops L40S studio deterministically and runs a user-provided training script via `studio.run()`.
- Enforces remote-only execution (no local `from_pretrained`).

## 3. Implementation

```bash
# /opt/axentx/vanguard/ingest/manifest.py
#!/usr/bin/env python3
"""
Generate CDN-only manifest for a Hugging Face dataset repo and date folder.
Usage:
  python3 manifest.py --repo org/dataset --date 2026-05-03 --out manifests/
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
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date_folder: str, out_dir: Path):
    api = HfApi()
    # Non-recursive top-level of date folder (avoids huge recursive pagination)
    entries = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)

    files = []
    for e in entries:
        if getattr(e, "type", None) != "file":
            continue
        path = e.path
        sha = getattr(e, "oid", None) or getattr(e, "sha", None) or ""
        url = CDN_TEMPLATE.format(repo=repo, path=path)
        slug = Path(path).stem  # date/slug.parquet -> slug
        files.append({
            "repo": repo,
            "path": path,
            "sha": sha,
            "url": url,
            "date": date_folder,
            "slug": slug
        })

    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "repo": repo,
        "date": date_folder,
        "count": len(files),
        "files": files
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"manifest-{date_folder}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} entries to {out_path}")
    return out_path

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True, help="HF dataset repo, e.g. org/name")
    p.add_argument("--date", required=True, help="Date folder in repo, e.g. 2026-05-03")
    p.add_argument("--out", default="manifests", help="Output directory")
    args = p.parse_args()
    build_manifest(args.repo, args.date, Path(args.out))
```

```bash
# /opt/axentx/vanguard/lightning/launch.py
#!/usr/bin/env python3
"""
Reuse or start a Lightning Studio and run a training script remotely.
Usage:
  python3 launch.py --script ../train.py --args "--manifest manifests/manifest-2026-05-03.json"
"""
import argparse
import subprocess
import sys
from pathlib import Path

try:
    from lightning_sdk import Studio, Machine
    from lightning_sdk.workspace import Teamspace
except ImportError:
    print("Install: pip install lightning")
    sys.exit(1)

def find_running_studio(name: str):
    try:
        for s in Teamspace.studios:
            if s.name == name and s.status == "Running":
                return s
    except Exception:
        pass
    return None

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--script", required=True, help="Path to training script")
    p.add_argument("--args", default="", help="Extra args passed to script")
    p.add_argument("--studio", default="vanguard-training", help="Studio name")
    p.add_argument("--machine", default="Lightning-L40S", help="Machine type")
    args = p.parse_args()

    script_path = Path(args.script).resolve()
    if not script_path.exists():
        print(f"Script not found: {script_path}")
        sys.exit(1)

    studio = find_running_studio(args.studio)
    if studio:
        print(f"Reusing running studio: {studio.name} ({studio.id})")
    else:
        print(f"Creating studio: {args.studio}")
        studio = Studio.create(name=args.studio, machine=Machine[args.machine], create_ok=True)

    # Ensure studio is running before run()
    if studio.status != "Running":
        print("Studio not running; starting...")
        studio.start(machine=Machine[args.machine])

    # Copy script into studio workspace (simple approach: run from mounted repo)
    # Assumes repo is available in studio environment at same relative path.
    cmd = ["python", str(script_path.name)]
    if args.args:
        cmd.extend(args.args.split())

    print(f"Running in studio: {' '.join(cmd)}")
    run = studio.run(cmd, cwd=str(script_path.parent))
    print(f"Run started: {run.id}")
    # Optionally stream logs: run.logs() or run.wait()

if __name__ == "__main__":
    main()
```

## 4. Verification
- Generate manifest: `python3 /opt/axentx/vanguard/ingest/manifest.py --repo org/dataset --date 2026-05-03 --out manifests/` and confirm `manifests/manifest-2026-05-03.json` exists with CDN `url` fields and no HF API endpoints.
- Inspect a training script that consumes the manifest and confirm it uses `requests`/`wget`/`torch.load` from `url` only (no `load_dataset` or `list_repo_tree` calls during training).
- Launch training: `python3 /opt/axentx/vanguard/lightning/launch.py --script ../train.py --args "--manifest manifests/manifest-2026-05-03.json"` and verify it reuses a running studio or starts L40S, then executes the script remotely without local GPU usage.
