# airship / frontend

## Final Consolidated Implementation (≤2h)

**Embed a CDN-only file manifest into the Surrogate-1 training pipeline** so Lightning Studio training runs with **zero HF API calls during data loading**, eliminating 429 rate limits and 128-commit caps while keeping Mac as orchestrator-only.

**Why this ships in <2h**:  
- Single Python script + one-line change in `surrogate/train.py`  
- No infra changes, no new deps, no GPU time burned on trial/error  
- Reuses existing HF repo layout and Lightning Studio reuse pattern  
- Uses CDN-only `resolve/main/` URLs to bypass auth/rate-limits entirely  

---

## Implementation Plan

### 1. Add manifest builder (Mac orchestrator)
Path: `/opt/axentx/airship/tools/build_manifest.py`

Responsibilities:
- Run **once per date folder** after ingestion completes (or on a cron after `knowledge-rag`/`granite-business-research.sh`)
- Use `list_repo_tree(path, recursive=False)` per folder to avoid pagination
- Emit `manifests/{date}/file_manifest.json` containing `{repo, path, sha, size, url}` for every parquet in the date folder
- Commit manifest to the same repo (tiny file, minimal commit pressure)

```python
#!/usr/bin/env python3
"""
tools/build_manifest.py
Generate CDN-only manifest for Surrogate-1 training.
Run on Mac orchestrator after ingestion.
"""
import json, os, sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi, Repository

REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-ingest")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
OUT_DIR = Path(os.getenv("MANIFEST_OUT_DIR", "manifests")) / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)

api = HfApi()

def build_manifest() -> None:
    entries = []
    folder_path = f"batches/mirror-merged/{DATE_FOLDER}"
    # One API call per folder depth (non-recursive) to avoid pagination hell
    for item in api.list_repo_tree(repo_id=REPO, path=folder_path, recursive=False):
        if not item.path.endswith(".parquet"):
            continue
        entries.append({
            "repo": REPO,
            "path": item.path,
            "sha": getattr(item, "oid", None),
            "size": getattr(item, "size", None),
            # CDN URL: no Authorization header required
            "url": f"https://huggingface.co/datasets/{REPO}/resolve/main/{item.path}",
        })

    out_path = OUT_DIR / "file_manifest.json"
    manifest = {
        "date": DATE_FOLDER,
        "generated_at": datetime.utcnow().isoformat(),
        "entries": entries
    }
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(entries)} entries to {out_path}")

    # Optional: upload manifest to repo (counts toward commit cap; keep small)
    if os.getenv("UPLOAD_MANIFEST", "1") == "1":
        repo = Repository(local_dir=str(OUT_DIR.parent.parent), clone_from=REPO)
        repo.git_add(str(out_path.relative_to(OUT_DIR.parent.parent)))
        repo.commit(f"chore: manifest {DATE_FOLDER}")
        repo.push_to_hub()
        print("Manifest pushed to repo.")

if __name__ == "__main__":
    build_manifest()
```

Make executable and ensure Bash wrapper safety (per past lessons):
```bash
chmod +x /opt/axentx/airship/tools/build_manifest.py
# If invoked via cron, ensure:
# SHELL=/bin/bash
# * * * * * /usr/bin/env bash /opt/axentx/airship/tools/build_manifest.py >> /var/log/airship/manifest.log 2>&1
```

---

### 2. Update training script to use CDN-only manifest
Path: `/opt/axentx/airship/surrogate/train.py`

Changes:
- Accept `--manifest` arg pointing to `manifests/{date}/file_manifest.json`
- Replace `load_dataset(streaming=True, repo_id=...)` with a `IterableDataset` that:
  - Reads manifest line-by-line
  - Streams each file via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth, no API)
  - Projects to `{prompt, response}` only at parse time (avoids pyarrow `CastError` on mixed schemas)
- Keep HF API usage to **zero** inside training loop

Minimal patch:

```python
# surrogate/train.py  (excerpt)
import json, argparse, requests, pyarrow.parquet as pq
from io import BytesIO
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.entries = self.manifest["entries"]

    def __iter__(self):
        for e in self.entries:
            # CDN fetch: zero HF API calls, bypasses auth/rate-limit
            resp = requests.get(e["url"], timeout=30)
            resp.raise_for_status()
            table = pq.read_table(BytesIO(resp.content))
            # Project to {prompt, response} only (mixed-schema safe)
            for batch in table.to_batches():
                cols = {k: batch.column(k) for k in ("prompt", "response") if k in batch.schema.names}
                if len(cols) != 2:
                    continue
                # yield tokenized example (pseudo)
                yield {"prompt": cols["prompt"][0].as_py(), "response": cols["response"][0].as_py()}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="manifests/manifest-2026-05-03.json")
    args = parser.parse_args()

    dataset = CDNParquetDataset(args.manifest)
    # ... Lightning DataModule / train loop unchanged
```

---

### 3. Studio reuse guard (per past lessons)
Before each `.run()` in orchestration scripts, add:

```python
from lightning import Teamspace, Studio, Machine

def get_or_start_studio(name: str) -> Studio:
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            return s
    return Studio(name=name, create_ok=True).start(machine=Machine.L40S)
```

---

## Rollout Checklist (≤2h)
- [ ] `tools/build_manifest.py` created + executable + shebang/cron safety  
- [ ] One test run on yesterday’s date folder → `manifests/{date}/file_manifest.json` produced  
- [ ] `surrogate/train.py` patched to accept `--manifest` and use `CDNParquetDataset`  
- [ ] Smoke test: Lightning Studio run with manifest → zero HF API calls in logs  
- [ ] Commit and tag (e.g., `v0.3.0-cdn-manifest`)  

**Expected outcome**: Training jobs no longer hit HF API 429/128-hr limits; Mac remains orchestrator-only; CDN fetches unlimited.
