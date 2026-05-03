# airship / frontend

### Final Consolidated Implementation  
*(Best parts merged; contradictions resolved for correctness + concrete actionability)*

---

## Core Improvements (≤2h)

1. **CDN-only training manifest**  
   - Generate once on the Mac (no per-worker HF API calls).  
   - Embed in `train.py`; workers stream directly from `https://huggingface.co/datasets/.../resolve/main/...` (public CDN URLs).  
   - Eliminates HF rate-limit exposure during training.

2. **Lightning idle-resilient launcher**  
   - Before every training run, check Lightning Studio status.  
   - If stopped/idle, restart on an L40S machine, then launch training.  
   - Prevents silent kills from idle timeouts.

---

## Implementation Plan (<2h)

| Step | Owner | Time | Deliverable |
|------|-------|------|-------------|
| 1. Inspect current `train.py` and data loader | me | 10m | confirm entrypoint |
| 2. Add `tools/gen_cdn_manifest.py` (Mac-side) | me | 20m | `manifests/YYYY-MM-DD.json` with CDN URLs |
| 3. Patch `train.py` to accept `--manifest` and use CDN-only fetches | me | 30m | zero HF API calls during training |
| 4. Add Lightning idle-resilient launcher (`tools/launch_lightning_studio.py`) | me | 20m | status check + restart + run |
| 5. Add cron-safe wrapper `tools/run_training.sh` (shebang + executable) | me | 10m | `SHELL=/bin/bash` compatible |
| 6. Smoke test (dry-run manifest + dummy dataloader) | me | 20m | verify CDN URLs resolve and loader streams |
| 7. Commit + docs (`docs/training_cdn.md`) | me | 10m | usage + rate-limit bypass notes |

---

## Code Snippets

### 1. `tools/gen_cdn_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate CDN-only manifest for a HF dataset repo/date folder.
Usage: python tools/gen_cdn_manifest.py <repo> <date_folder> <out_json>
"""
import json
import os
import sys
import urllib.request
import re
from typing import List, Dict

def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else "oxford-llm/surrogate-mirror"
    date_dir = sys.argv[2] if len(sys.argv) > 2 else "2026-04-29"
    out = sys.argv[3] if len(sys.argv) > 3 else f"manifests/{date_dir}.json"

    os.makedirs(os.path.dirname(out), exist_ok=True)

    # Try huggingface_hub first (one API call, non-recursive)
    try:
        from huggingface_hub import list_repo_tree
        items = list_repo_tree(repo_id=repo, path=date_dir, recursive=False)
        files = sorted(it.rfilename for it in items if it.type == "file" and it.rfilename.endswith(".parquet"))
    except Exception:
        # Fallback: public CDN directory listing (no auth)
        listing_url = f"https://huggingface.co/datasets/{repo}/tree/main/{date_dir}"
        req = urllib.request.Request(listing_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8")
        files = sorted(set(re.findall(rf'{re.escape(date_dir)}/([^"]+\.parquet)"', html)))

    base_cdn = f"https://huggingface.co/datasets/{repo}/resolve/main/{date_dir}"
    manifest: List[Dict[str, str]] = [
        {"filename": f, "cdn_url": f"{base_cdn}/{f}", "local_path": f"{date_dir}/{f}"}
        for f in files
    ]

    with open(out, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(manifest)} files to {out}")

if __name__ == "__main__":
    main()
```
Make executable:
```bash
chmod +x tools/gen_cdn_manifest.py
```

---

### 2. `train.py` patch
```python
# train.py (excerpt)
import argparse
import json
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset, DataLoader
from typing import List, Dict

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path: str, columns=("prompt", "response")):
        with open(manifest_path) as f:
            self.manifest: List[Dict] = json.load(f)
        self.columns = columns

    def _stream_parquet(self, cdn_url: str):
        with requests.get(cdn_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_content(chunk_size=131072):
                buf.extend(chunk)
            import io
            table = pq.read_table(io.BytesIO(buf), columns=self.columns)
            for batch in table.to_batches(max_chunksize=1024):
                for i in range(batch.num_rows):
                    row = {col: batch[col][i].as_py() for col in self.columns}
                    yield row

    def __iter__(self):
        for item in self.manifest:
            yield from self._stream_parquet(item["cdn_url"])

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to CDN manifest JSON")
    args = parser.parse_args()

    dataset = CDNParquetDataset(args.manifest)
    loader = DataLoader(dataset, batch_size=8, num_workers=0)

    for batch in loader:
        # training step
        pass
```

---

### 3. `tools/launch_lightning_studio.py`
```python
#!/usr/bin/env python3
"""
Lightning idle-resilient launcher.
Checks studio status, restarts if stopped, then runs training.
"""
import subprocess
import time
import lightning

def ensure_studio_running(machine=lightning.Machine.L40S, max_retries=3):
    studio = lightning.Studio()
    for attempt in range(1, max_retries + 1):
        try:
            if studio.status != "Running":
                print(f"Studio not running (status={studio.status}). Restarting...")
                studio.start(machine=machine)
            return studio
        except Exception as e:
            print(f"Attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                time.sleep(10)
            else:
                raise

def run_training(script="train.py", manifest="manifests/latest.json", extra_args=None):
    cmd = ["python", script, "--manifest", manifest]
    if extra_args:
        cmd.extend(extra_args)
    subprocess.run(cmd, check=True)

if __name__ == "__main__":
    ensure_studio_running()
    run_training()
```

---

### 4. `tools/run_training.sh`
```bash
#!/usr/bin/env bash
# Cron-safe wrapper for Lightning training with CDN manifest.
# Add to crontab:
#   SHELL=/bin/bash
#   0 2 * * * /opt/axentx/airship/surrogate/tools/run_training.sh >> /var/log/surrogate_training.log 2>&1

set -euo pipefail
cd "$(dirname "$0")/.."

MANIFEST_DATE=$(date +%Y-%m-%d)
MANIFEST="manifests/${MANIFEST_DATE}.json"

# Generate fresh manifest each run (Mac-side, one API call)
python3 tools/gen_cdn_manifest.py oxford-llm/surrogate-mirror "$MANIFEST_DATE" "$MANIFEST"

# Launch Lightning studio (idle-resilient) and run training
python3 tools/launch_lightning_studio.py
```
Make executable:
```bash
chmod +x tools/run_training.sh
```

---

## Docs (`docs/training_cdn.md`)
```markdown
# CDN-only Training (HF rate-limit bypass)

## How it works
- `tools/gen_cdn_manifest.py` produces `manifests/YYYY-MM
