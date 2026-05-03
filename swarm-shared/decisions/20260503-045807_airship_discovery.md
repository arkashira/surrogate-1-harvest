# airship / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

**Highest-value incremental improvement (≤2h):**  
Embed a **CDN-only file manifest** into the Surrogate-1 training pipeline so Lightning Studio training runs with **zero HF API calls during data loading**, eliminating 429 rate limits and 128-commit caps while keeping Mac as orchestrator-only.

- **Why this now**:  
  - Directly applies the CDN-bypass insight.  
  - Fits in <2h: one manifest generator + small train.py patch + optional cron.  
  - Removes the biggest runtime failure modes (429s, ingestion commit cap) for training loops.

---

## Implementation Plan (Corrected + Actionable)

1. **Add manifest generator** (`tools/build_manifest.py`)  
   - Runs on Mac (or any orchestrator) after rate-limit window clears.  
   - Uses **one `list_repo_tree(..., recursive=True)`** per date folder (corrected: recursive required to list files inside nested folders).  
   - Emits `manifests/{dataset_repo}/{date}/filelist.json` with CDN URLs, local basenames, and sizes.  
   - Stores only `{cdn_url, local_name, size}` (no API calls later).

2. **Patch training script** (`surrogate/train.py`)  
   - Accept `--manifest` path.  
   - During `DataLoader` init, reads manifest and streams via `requests.get(cdn_url, timeout=60)` with retries.  
   - Projects to `{prompt, response}` on the fly (avoids pyarrow schema issues).  
   - **Zero `load_dataset`, zero `hf_hub_download`, zero API calls during training.**

3. **Lightning Studio integration**  
   - Reuse running studio when possible (`Teamspace.studios` check).  
   - Pass manifest path as script argument or mount via cloud storage (s3/gcs) or artifact.  
   - If studio stopped, restart with `target.start(machine=Machine.L40S)` before `.run()`.  
   - **Corrected snippet** (fixes truncated code from Candidate 1):

   ```python
   from lightning import Studio, Teamspace, Machine

   team = Teamspace()
   studio_name = "surrogate-train-l40s"
   running = next((s for s in team.studios if s.name == studio_name), None)

   target = None
   if running and running.status == "running":
       target = running
       print(f"Reusing running studio: {studio_name}")
   else:
       target = Studio(name=studio_name, machine=Machine.L40S)
       target.start()
       print(f"Started studio: {studio_name}")

   target.run(
       script="surrogate/train.py",
       arguments=["--manifest", "manifests/datasets/my-org/surrogate-mirror/2026-04-29/filelist.json"],
   )
   ```

4. **Optional automation**  
   - Add cron-friendly wrapper with proper shebang and `SHELL=/bin/bash`.  
   - Log rotation for manifest builds.

---

## Code Snippets (Best + Corrected)

### tools/build_manifest.py
```python
#!/usr/bin/env python3
"""
Build a CDN-only manifest for a HuggingFace dataset repo folder.
Usage:
  python tools/build_manifest.py \
    --repo datasets/my-org/surrogate-mirror \
    --date 2026-04-29 \
    --out manifests/my-org/surrogate-mirror/2026-04-29/filelist.json
"""

import argparse
import json
import os
import time
from pathlib import Path
from typing import List, Dict

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date_folder: str, out_path: Path) -> Dict:
    api = HfApi()
    # Single recursive tree call per date folder (corrected)
    entries = api.list_repo_tree(repo=repo, path=date_folder, recursive=True)

    files = []
    total_size = 0
    for e in entries:
        if e.type != "file":
            continue
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=e.path)
        size = getattr(e, "size", 0)
        files.append({
            "cdn_url": cdn_url,
            "local_name": os.path.basename(e.path),
            "size": size,
        })
        total_size += size

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_files": len(files),
        "total_bytes": total_size,
        "files": files,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files -> {out_path}")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN-only manifest for HF dataset folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/my-org/surrogate-mirror)")
    parser.add_argument("--date", required=True, help="Date folder inside repo (e.g. 2026-04-29)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    build_manifest(args.repo, args.date, Path(args.out))
```

### surrogate/train.py (minimal patch)
```python
import argparse
import json
import time
from pathlib import Path
from typing import Iterator, Tuple

import requests
import torch
from torch.utils.data import IterableDataset, DataLoader

class CDNTextDataset(IterableDataset):
    def __init__(self, manifest_path: Path, max_retries: int = 3, timeout: int = 60):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = self.manifest["files"]
        self.max_retries = max_retries
        self.timeout = timeout

    def _stream_file(self, cdn_url: str) -> str:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(cdn_url, timeout=self.timeout, stream=True)
                resp.raise_for_status()
                return resp.text
            except Exception as exc:
                if attempt == self.max_retries:
                    raise
                sleep_sec = 2 ** attempt
                print(f"Retry {attempt}/{self.max_retries} for {cdn_url}: {exc}. Sleeping {sleep_sec}s")
                time.sleep(sleep_sec)

    def _parse_record(self, text: str) -> Tuple[str, str]:
        # Placeholder: project text -> (prompt, response)
        # Replace with your actual parsing logic (e.g., JSONL, parquet projection)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) >= 2:
            return lines[0], lines[1]
        return "", ""

    def __iter__(self) -> Iterator[dict]:
        for item in self.files:
            text = self._stream_file(item["cdn_url"])
            prompt, response = self._parse_record(text)
            if prompt and response:
                yield {"prompt": prompt, "response": response}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True, help="Path to manifest JSON")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    dataset = CDNTextDataset(args.manifest)
    loader = DataLoader(dataset, batch_size=args.batch_size, num_workers=args.workers)

    # Example training loop stub
    for batch in loader:
        # Replace with actual surrogate training step
        print(f"Batch keys: {list(batch.keys())}, batch size: {len(batch['prompt'])}")
        # train_step(batch)

if __name__ == "__main__":
    main()
```

### Wrapper for cron (optional)
`tools/build_manifest.sh`
```bash
#!/usr/bin/env bash
set -eu
