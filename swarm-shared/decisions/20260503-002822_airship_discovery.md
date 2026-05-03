# airship / discovery

## Final Synthesis: CDN-Only Manifest Dataset Loader for Surrogate Training

**Core Value (merged)**: Eliminates HF API 429 rate limits during training, removes quota burn on Lightning Studio, enables zero-API-call training, and ensures reliable long-running jobs by replacing streaming dataset dependencies with CDN-backed iteration.

---

## Implementation Plan (≤2h)

### 1. Locate training ingestion code (10–30m)
Find surrogate training scripts using:
- `load_dataset(streaming=True)`
- `list_repo_files(..., recursive=True)`
- HF dataset streaming inside training loops

**Action**: Identify the exact entrypoint(s) and confirm schema expectations (`prompt`, `response`).

---

### 2. Create manifest generator (30–45m)
**Run on Mac orchestrator once per date folder after rate-limit window clears.**

Key rules:
- Use `list_repo_tree(path, recursive=False)` per date folder (single API call per folder).
- Do **not** recurse; iterate date folders explicitly.
- Emit `file_manifest.json` with CDN URLs only:  
  `https://huggingface.co/datasets/{repo}/resolve/main/{path}`
- Filter to `.parquet` files under `enriched/` or `batches/mirror-merged/{date}/`.
- Include minimal metadata (`path`, `cdn_url`, `size`).

**Correctness fix (vs Candidate 1)**: Avoid assuming `list_repo_tree` returns `size`; handle missing attributes gracefully.

```python
#!/usr/bin/env python3
"""
Generate CDN-only file manifest for surrogate training.
Run on Mac orchestrator (single API burst after rate-limit window).
"""
import json
import os
from datetime import datetime, timedelta
from huggingface_hub import HfApi

api = HfApi()
REPO_ID = "axentx/surrogate-enriched"
OUT_DIR = "manifests"
os.makedirs(OUT_DIR, exist_ok=True)

def gen_manifest(date_str: str):
    folder_path = f"batches/mirror-merged/{date_str}"
    entries = api.list_repo_tree(REPO_ID, path=folder_path, recursive=False)

    files = []
    for e in entries:
        if getattr(e, "path", "").endswith(".parquet"):
            cdn_url = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{e.path}"
            files.append({
                "path": e.path,
                "cdn_url": cdn_url,
                "size": getattr(e, "size", None)
            })

    # Sort for deterministic ordering
    files.sort(key=lambda x: x["path"])

    manifest = {
        "repo": REPO_ID,
        "date": date_str,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": files,
        "total_files": len(files)
    }

    out_path = os.path.join(OUT_DIR, f"manifest_{date_str}.json")
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {out_path} ({len(files)} files)")
    return out_path

if __name__ == "__main__":
    for i in range(7):
        d = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            gen_manifest(d)
        except Exception as exc:
            print(f"Skip {d}: {exc}")
```

---

### 3. Replace dataset loader with CDN iterable (45m)
**Deploy inside Lightning training environment.**

Requirements:
- Zero HF API calls during training.
- Stream parquet via CDN URLs with `requests` (no auth).
- Project to `{prompt, response}` at parse time.
- Handle mixed/missing schemas gracefully (skip invalid files).
- Deterministic iteration order (follow manifest order).
- Lightweight error handling: log and continue on individual file failures.

```python
#!/usr/bin/env python3
"""
CDN-only iterable dataset for surrogate training.
Zero HF API calls during training.
"""
import io
import json
import logging
from typing import Dict, Iterable, Optional

import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

logger = logging.getLogger(__name__)

class CdnIterableDataset(IterableDataset):
    def __init__(self, manifest_path: str, max_files: Optional[int] = None):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = self.manifest["files"]
        if max_files:
            self.files = self.files[:max_files]
        logger.info(f"CDN dataset: {len(self.files)} files from {self.manifest['repo']}")

    def _stream_parquet(self, cdn_url: str) -> Iterable[Dict]:
        try:
            resp = requests.get(cdn_url, timeout=30)
            resp.raise_for_status()
            buf = io.BytesIO(resp.content)
            table = pq.read_table(buf)

            has_prompt = "prompt" in table.column_names
            has_response = "response" in table.column_names

            if not (has_prompt and has_response):
                logger.warning(f"Missing prompt/response in {cdn_url}, skipping")
                return

            prompts = table["prompt"].to_pylist()
            responses = table["response"].to_pylist()

            for p, r in zip(prompts, responses):
                if p and r:
                    yield {"prompt": str(p), "response": str(r)}

        except Exception as exc:
            logger.error(f"Failed to stream {cdn_url}: {exc}")

    def __iter__(self) -> Iterable[Dict]:
        for fmeta in self.files:
            yield from self._stream_parquet(fmeta["cdn_url"])
```

---

### 4. Update training script and Lightning Studio launcher (30m)

**Training script change**:
```python
# Replace:
# from datasets import load_dataset
# dataset = load_dataset("axentx/surrogate-enriched", streaming=True)

# With:
from data.cdn_dataset import CdnIterableDataset
import os

manifest_path = os.getenv("CDN_MANIFEST_PATH", "manifests/manifest_latest.json")
dataset = CdnIterableDataset(manifest_path=manifest_path)

from torch.utils.data import DataLoader
train_loader = DataLoader(dataset, batch_size=8, num_workers=0)
```

**Lightning Studio launcher guard (actionable fix)**:
- Before `.run()`, check studio status.
- If not `'running'`, restart it.
- Mount manifest as volume or bake into image.

```python
# Example guard in launcher script
if studio.status != "running":
    studio.stop()
    studio.start()
    studio.wait_until_running()
```

**Dockerfile update**:
```dockerfile
COPY manifests/ /app/manifests/
ENV CDN_MANIFEST_PATH=/app/manifests/manifest_latest.json
```

---

### 5. Test locally and deploy (30m)

**Local test checklist**:
- Run manifest generator and verify JSON structure.
- Instantiate `CdnIterableDataset` and iterate a few items.
- Confirm no HF API calls (e.g., monitor logs or use `httpx` mock).
- Run one training step with `train_loader` to validate shapes/types.

**Deployment steps**:
1. On Mac orchestrator: run `python gen_manifest.py` for target dates.
2. Copy or mount `manifests/` into surrogate training container.
3. Update training script to use `CdnIterableDataset`.
4. Add studio status guard in launcher.
5. Run a short training job locally, then deploy to Lightning Studio.

---

## Resolved Contradictions (in favor of correctness + actionability)

| Contradiction | Resolution |
|--------------|------------|
| Manifest generation recursion scope | Use `recursive=False` per date folder; do not recurse inside generator. Iterate date folders explicitly. |
| Handling of missing `size` attribute | Use `getattr(e, "size", None)`; do not assume field exists. |
| Schema robustness | Explicitly check for `prompt` and `response` columns; skip files if missing. |
| Studio lifecycle | Add explicit guard: restart studio if not `'running'` before `.run()`. |
| Determinism | Sort manifest files by path to ensure repeatable ordering. |

---

## Expected Outcome
- Training proceeds with **zero HF API calls**, eliminating 429 errors and quota consumption.
