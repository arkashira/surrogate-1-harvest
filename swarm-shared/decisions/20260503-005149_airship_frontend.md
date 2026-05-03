# airship / frontend

**Final Consolidated Implementation Plan**  
*(Best of Candidate 1 + Candidate 2, de-duplicated and hardened for correctness + actionability)*

---

## Goal
Eliminate HF API rate limits and `pyarrow.CastError` in Surrogate-1 training by switching to a **manifest-driven, CDN-only dataset loader** that can be run locally or in Lightning Studio with zero HF API calls during training.

---

## High-level Design Decisions (resolve contradictions)
- **Single source of truth**: manifest is generated **once per date folder** on the Mac orchestrator (not per training run) → avoids repeated API calls and race conditions.
- **CDN-only during training**: training script never uses `datasets.load_dataset` or HF API; uses direct CDN URLs (`resolve/main/...`).
- **Schema robustness**: explicitly select only `prompt`/`response` columns; ignore extra fields to avoid `pyarrow.CastError`.
- **Retry + skip**: transient CDN failures are retried; corrupt/unreadable files are logged and skipped to avoid crashing training.
- **Lightning Studio reuse guard**: prevent duplicate studio creation; restart idle/stopped studios instead of creating new ones (saves quota and time).

---

## Implementation Plan (≤2h)

| Step | Owner | Time | Deliverable |
|------|-------|------|-------------|
| 1. Add file-listing script (Mac) | Orchestrator | 15m | `scripts/list_hf_date_folder.py` → `file-list.json` |
| 2. Add CDN-only dataset loader | Training | 45m | `surrogate/training/data/cdn_dataset.py` |
| 3. Update training entrypoint | Training | 30m | Replace `load_dataset` with `CdnDataset` |
| 4. Add Studio reuse guard | Orchestrator | 15m | Check running studios; restart if stopped/idle |
| 5. Smoke test (local + studio) | Training | 15m | Validate small manifest + 10 files parse correctly |

---

## Code (final, ready to copy)

### 1. `scripts/list_hf_date_folder.py` (Mac orchestration)

```python
#!/usr/bin/env python3
"""
List parquet files in a date folder of a HF dataset repo.
Usage:
    python list_hf_date_folder.py \
        --repo axentx/surrogate-mirror \
        --date 2026-04-29 \
        --out file-list.json
"""
import argparse
import json
import os
import time
from pathlib import Path
from huggingface_hub import HfApi

HF_TOKEN = os.getenv("HF_TOKEN")
api = HfApi(token=HF_TOKEN)

def list_date_folder(repo: str, date: str, out_path: str):
    prefix = f"batches/mirror-merged/{date}/"
    print(f"Listing {repo} @ {prefix}")

    entries = api.list_repo_tree(repo=repo, path=prefix, recursive=False)
    files = [e for e in entries if e.type == "file" and e.path.endswith(".parquet")]

    base_cdn = f"https://huggingface.co/datasets/{repo}/resolve/main/"
    manifest = {
        "repo": repo,
        "date": date,
        "base_cdn": base_cdn,
        "files": [
            {
                "path": f.path,
                "cdn_url": f"{base_cdn}{f.path}",
                "size": getattr(f, "size", None),
            }
            for f in files
        ],
    }

    Path(out_path).write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--out", default="file-list.json")
    args = p.parse_args()

    try:
        list_date_folder(args.repo, args.date, args.out)
    except Exception as e:
        if "429" in str(e):
            print("Rate limited — waiting 360s")
            time.sleep(360)
            list_date_folder(args.repo, args.date, args.out)
        else:
            raise
```

---

### 2. `surrogate/training/data/cdn_dataset.py`

```python
import json
import pyarrow.parquet as pq
import requests
from io import BytesIO
from typing import Iterator, Dict, Any
from torch.utils.data import IterableDataset

CDN_TIMEOUT = 30

class CdnDataset(IterableDataset):
    """
    CDN-only dataset loader for Surrogate-1 training.
    Manifest format:
    {
      "repo": "...",
      "date": "...",
      "base_cdn": "...",
      "files": [{"path": "...", "cdn_url": "...", "size": ...}, ...]
    }
    """

    def __init__(self, manifest_path: str, columns=("prompt", "response"), max_retries=3):
        super().__init__()
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = self.manifest["files"]
        self.columns = columns
        self.max_retries = max_retries

    def _download_parquet(self, cdn_url: str) -> bytes:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(cdn_url, timeout=CDN_TIMEOUT)
                resp.raise_for_status()
                return resp.content
            except Exception as exc:
                if attempt == self.max_retries:
                    raise RuntimeError(f"Failed to fetch {cdn_url}: {exc}") from exc
                import time
                time.sleep(2 ** attempt)

    def _parse_file(self, data: bytes) -> Iterator[Dict[str, Any]]:
        table = pq.read_table(BytesIO(data), columns=self.columns)
        df = table.to_pandas()
        for _, row in df.iterrows():
            yield {k: row[k] for k in self.columns if k in row}

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for item in self.files:
            cdn_url = item["cdn_url"]
            try:
                data = self._download_parquet(cdn_url)
                yield from self._parse_file(data)
            except Exception as exc:
                print(f"Skipping {cdn_url}: {exc}")
                continue
```

---

### 3. Update training entrypoint

Replace:
```python
from datasets import load_dataset
ds = load_dataset("axentx/surrogate-mirror", split="train", streaming=True)
```

With:
```python
from surrogate.training.data.cdn_dataset import CdnDataset
ds = CdnDataset(manifest_path="file-list.json")
```

(Ensure `file-list.json` is present in the training working directory or pass the correct path.)

---

### 4. Lightning Studio reuse guard (orchestration snippet)

Before creating a new studio, check existing ones:

```bash
# Example guard logic (pseudo-shell)
EXISTING=$(lightning studio list --name surrogate-studio --status running,stopped --format json)
if [ -n "$EXISTING" ]; then
  echo "Found existing studio; restarting if stopped..."
  lightning studio restart <id>
else
  echo "Creating new studio..."
  lightning studio create ...
fi
```

---

### 5. Smoke test checklist
1. Generate `file-list.json` for a small date folder (≤10 files).
2. Run `CdnDataset(manifest_path="file-list.json")` locally and iterate 100 samples.
3. Confirm no HF API calls (`huggingface_hub` logs) and no `pyarrow.CastError`.
4. Push same manifest + training script to Lightning Studio and run one training step.

---

## Expected Outcome
- HF API rate limits bypassed during training.
- `pyarrow.CastError` avoided by selecting only required columns.
- Training works identically on Mac and Lightning Studio.
- Total implementation time ≤2h.
