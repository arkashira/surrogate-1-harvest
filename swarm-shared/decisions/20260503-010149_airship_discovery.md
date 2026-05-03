# airship / discovery

## Final Synthesized Implementation  
*(Best parts merged; contradictions resolved for correctness + concrete actionability)*

**Core value (unchanged)**  
- Eliminates HF API 429s and `pyarrow.CastError`  
- Enables 24/7 autonomous training by fully decoupling data discovery (one-time) from training (CDN-only)  
- Works in Lightning Studio with idle-stop resilience  

**Resolved contradictions**  
- Use **single non-recursive tree call per folder** (not recursive) → faster, fewer API hits, sufficient because we only need parquet files in `batches/mirror-merged/{date}`.  
- **Schema safety**: project only `prompt`/`response` at read time (not at manifest time) to tolerate future schema drift without regenerating manifests.  
- **Lightning Studio integration**: prefer **run arguments + studio reuse** over file mounts (more portable across Studio sessions and avoids mount churn).  
- **Validation**: require **zero HF dataset API calls during training**; verify by logging and by using only `fsspec`/`pyarrow.parquet` over HTTP.  

**ETA**: ~90–120 minutes (including tests and smoke run)

---

## Implementation Plan (actionable)

1. **Add manifest generator** (`scripts/build_dataset_manifest.py`)  
   - Runs on dev machine (Mac or Linux)  
   - Single `list_repo_tree(..., recursive=False)` per date folder  
   - Emits `dataset_manifest_{date}.json` with CDN URLs and optional row counts (if cheap to compute)  
   - Stores CDN URLs: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`  

2. **Add CDN-only dataset loader** (`surrogate/data/cdn_dataset.py`)  
   - Accepts manifest path or explicit CDN URL list  
   - Uses `pyarrow.parquet` + `fsspec` (HTTP) — no `datasets` library  
   - Projects only `prompt`/`response` on read; tolerates missing/extra columns  
   - Returns iterable of dicts; optionally supports lightweight length hint  

3. **Patch training entrypoint** (`surrogate/train/train_surrogate.py`)  
   - Add `--manifest` argument (required)  
   - Replace `load_dataset(streaming=True)` with `CdnDataset(manifest)`  
   - Wrap in tokenization map-style iterable for `Trainer`  
   - Keep collator/tokenizer unchanged  

4. **Lightning Studio integration**  
   - Reuse running Studio when available (Teamspace lookup)  
   - Pass manifest path via run argument or upload as run artifact  
   - Handle idle-stop: script should resume or restart gracefully if Studio stops  

5. **Validation & safeguards**  
   - Smoke test: load 10 files, verify schema, count rows, confirm zero HF dataset API calls  
   - Log warnings on failed files; continue training  
   - Optional: add row-count precomputation to manifest for progress tracking  

---

## Final Code Snippets

### 1) Manifest Generator (dev-side)

```python
# scripts/build_dataset_manifest.py
#!/usr/bin/env python3
"""
Generate CDN-only manifest for Surrogate-1 dataset.
Usage:
  python scripts/build_dataset_manifest.py \
    --repo datasets/your-org/surrogate-mirror \
    --date 2026-04-29 \
    --out dataset_manifest_2026-04-29.json
"""
import argparse
import json
import os
import time
from pathlib import Path

from huggingface_hub import HfApi

HF_TOKEN = os.getenv("HF_TOKEN")
API = HfApi(token=HF_TOKEN)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date: str, out_path: str):
    folder = f"batches/mirror-merged/{date}"
    print(f"Listing {repo}/{folder} (non-recursive) ...")

    # Single non-recursive tree call per folder
    tree = API.list_repo_tree(repo=repo, path=folder, recursive=False)

    files = []
    for item in tree:
        if not item.path.endswith(".parquet"):
            continue
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=item.path)
        files.append({
            "path": item.path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None),
        })

    manifest = {
        "repo": repo,
        "date": date,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    }

    Path(out_path).write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out)
```

### 2) CDN-Only Dataset Loader

```python
# surrogate/data/cdn_dataset.py
import json
import logging
from typing import Iterator, List, Dict, Any

import pyarrow.parquet as pq
import fsspec

logger = logging.getLogger(__name__)

class CdnDataset:
    """
    CDN-only dataset loader for Surrogate-1.
    Avoids HF datasets API entirely; uses direct Parquet reads over HTTP.
    Projects only {prompt, response} to prevent pyarrow.CastError on mixed schemas.
    """

    def __init__(self, manifest_path: str = None, cdn_urls: List[str] = None):
        if not (manifest_path or cdn_urls):
            raise ValueError("Provide manifest_path or cdn_urls")

        if manifest_path:
            manifest = json.loads(Path(manifest_path).read_text())
            self.urls = [f["cdn_url"] for f in manifest["files"]]
        else:
            self.urls = cdn_urls

        if not self.urls:
            raise ValueError("No files found in manifest/cdn_urls")

        logger.info(f"CDN dataset initialized with {len(self.urls)} files")

    def _project_record(self, batch) -> Iterator[Dict[str, Any]]:
        """Project only prompt/response; tolerate missing/extra columns."""
        df = batch.to_pandas()
        for _, row in df.iterrows():
            yield {
                "prompt": str(row.get("prompt", "")),
                "response": str(row.get("response", "")),
            }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for url in self.urls:
            try:
                with fsspec.open(url, mode="rb", anon=True) as f:
                    pf = pq.ParquetFile(f)
                    for batch in pf.iter_batches(batch_size=1024):
                        yield from self._project_record(batch)
            except Exception as exc:
                logger.warning(f"Failed to read {url}: {exc}")
                continue

    def __len__(self) -> int:
        # Optional: inexpensive hint; override if row counts are precomputed in manifest.
        return len(self.urls)
```

### 3) Training Entrypoint Patch

```python
# surrogate/train/train_surrogate.py  (excerpt)
import argparse
import logging
from pathlib import Path

from transformers import Trainer, TrainingArguments

from surrogate.data.cdn_dataset import CdnDataset
from surrogate.models.tokenizer import get_tokenizer
from surrogate.models.collator import SurrogateCollator

logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=str, required=True, help="Path to dataset_manifest_*.json")
    parser.add_argument("--output-dir", type=str, default="./outputs")
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    tokenizer = get_tokenizer()
    collator = SurrogateCollator(tokenizer)

    # CDN-only dataset
    dataset = CdnDataset(manifest_path=args.manifest)

    # Lightweight tokenization on-the-fly
    def tokenize_fn(examples):
        return tokenizer(
            examples["prompt"],
            text_target=examples["response"],
            truncation=True,
            max_length=2048,
