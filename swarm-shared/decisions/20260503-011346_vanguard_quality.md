# vanguard / quality

## 1. Diagnosis

- No persisted `(repo, dateFolder)` manifest exists → every page load and training run re-enumerates HF API via authenticated `list_repo_tree`/`/api/` calls, burning quota and risking 429s.
- Frontend and training scripts fetch file lists independently instead of sharing a single daily snapshot, causing redundant API traffic and inconsistent views.
- Data fetches use authenticated API endpoints instead of public CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`), missing the CDN bypass that avoids auth rate limits.
- No lightweight client-side cache or TTL mechanism to reuse the daily manifest, so even repeated frontend visits trigger fresh API calls.
- Training script likely re-lists folders on every epoch/step; this should be pre-computed once and embedded so Lightning training does CDN-only fetches with zero API calls during data load.

## 2. Proposed change

Add a daily manifest generator and CDN-based fetcher for the surrogate-1 dataset:

- Create `/opt/axentx/vanguard/scripts/build_dataset_manifest.py`
  - Runs on Mac (or cron) once per day after rate-limit window clears.
  - Calls `list_repo_tree(path, recursive=False)` for the target date folder.
  - Emits `vanguard/data/manifest-{date}.json` containing `{repo, dateFolder, files: [{path, cdn_url, sha256?}]}`.
- Update training script (`vanguard/train/train_surrogate1.py` or equivalent) to:
  - Accept `--manifest` arg pointing to the JSON file.
  - Use only CDN URLs from the manifest for data loading (no `load_dataset` or authenticated API calls during training).
  - Validate files exist locally or stream via CDN with `requests`/`urllib` and project to `{prompt, response}` on the fly.
- Update frontend data loader (likely `vanguard/frontend/src/lib/data.js` or similar) to:
  - Fetch `/data/manifest-latest.json` (symlink or copy) once per session or per day.
  - Use CDN URLs for file fetches; cache manifest in `localStorage` with 24h TTL.

## 3. Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/scripts /opt/axentx/vanguard/data /opt/axentx/vanguard/train
```

`/opt/axentx/vanguard/scripts/build_dataset_manifest.py`
```python
#!/usr/bin/env python3
"""
Build a daily manifest for surrogate-1 dataset using HF API once,
then rely on public CDN URLs for all downstream fetches.

Usage:
  python build_dataset_manifest.py --repo <datasets/xxx> --date-folder 2026-05-03 --out ./data/manifest-2026-05-03.json
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("ERROR: install huggingface_hub (pip install huggingface_hub)")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date_folder: str, out_path: Path):
    api = HfApi()
    folder_path = f"{date_folder}"  # relative path inside repo

    try:
        items = api.list_repo_tree(repo=repo, path=folder_path, recursive=False)
    except Exception as e:
        print(f"HF API error: {e}")
        # If rate-limited, fail fast — caller should retry after window
        raise

    files = []
    for item in items:
        if getattr(item, "type", None) != "file":
            continue
        path = f"{folder_path}/{item.path}" if not item.path.startswith(folder_path) else item.path
        files.append({
            "path": path,
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=path),
            "size": getattr(item, "size", None),
        })

    manifest = {
        "repo": repo,
        "dateFolder": date_folder,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote manifest with {len(files)} files to {out_path}")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build daily dataset manifest for CDN-based training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo, e.g. datasets/axentx/surrogate1")
    parser.add_argument("--date-folder", required=True, help="Date folder inside repo, e.g. 2026-05-03")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    build_manifest(args.repo, args.date_folder, Path(args.out))
```

`/opt/axentx/vanguard/train/train_surrogate1.py` (minimal change — embed manifest-driven CDN loader)
```python
#!/usr/bin/env python3
"""
Surrogate-1 training entrypoint that uses a pre-built manifest
and CDN-only fetches to avoid HF API calls during training.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Iterator, Tuple

import torch
from torch.utils.data import IterableDataset, DataLoader

try:
    import requests
except ImportError:
    print("ERROR: install requests")
    sys.exit(1)

class CDNTextDataset(IterableDataset):
    """Stream files from CDN URLs listed in manifest and yield {prompt, response}."""

    def __init__(self, manifest_path: Path, max_files: int = -1):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.files = [item["cdn_url"] for item in manifest["files"]]
        if max_files > 0:
            self.files = self.files[:max_files]

    def _stream_url(self, url: str) -> str:
        # CDN downloads do not count against API rate limits
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.text

    def _parse_to_pair(self, text: str) -> Tuple[str, str]:
        # Placeholder: project raw file to {prompt, response}.
        # Replace with your actual schema projection.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) >= 2:
            return lines[0], lines[1]
        return "", text

    def __iter__(self) -> Iterator[dict]:
        for url in self.files:
            try:
                raw = self._stream_url(url)
                prompt, response = self._parse_to_pair(raw)
                if prompt or response:
                    yield {"prompt": prompt, "response": response}
            except Exception as exc:
                # Log and continue to avoid crashing training on single bad file
                print(f"Skipping {url}: {exc}")
                continue

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--max-files", type=int, default=-1)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--steps", type=int, default=100)
    args = parser.parse_args()

    dataset = CDNTextDataset(Path(args.manifest), max_files=args.max_files)
    loader = DataLoader(dataset, batch_size=args.batch_size)

    # Minimal training loop placeholder
    step = 0
    for batch in loader:
        # Replace with real model forward/backward
        print(f"Step {step}: batch keys={list(batch.keys())}, prompt-len={len(batch['prompt'][0]) if batch['prompt'] else 0}")
        step += 1
        if step >= args.steps:
            break

    print("Training loop finished (placeholder).")

if __name__ == "__main__":
    main()
```

Make scripts executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build_dataset_manifest.py
chmod +x /opt/axentx/vanguard/train/train
