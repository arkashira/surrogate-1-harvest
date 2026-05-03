# airship / discovery

## Final Implementation Plan — Manifest-Driven, CDN-Only Iterable Loader

**Goal (≤2 h):** Eliminate HF API rate limits and `pyarrow.CastError` from mixed-schema repos by replacing `load_dataset`/`list_repo_files` with a manifest-driven, CDN-only streaming loader that is correct, minimal, and production-ready.

---

### 1. High-value change (what we ship)
- Add `tools/manifest_loader.py` to generate `file_manifest.json` once (on your Mac) after rate-limit window clears.
- Add `surrogate/data/manifest_dataset.py` with a `ManifestCdnDataset(IterableDataset)` that:
  - Uses only public CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`).
  - Never calls the HF Datasets API or `list_repo_files` during training.
  - Projects only `{prompt, response}` at parse time to avoid schema mismatches.
  - Retries CDN 429 with exponential backoff and longer waits for rate limits.
- Update the surrogate training entrypoint to use `ManifestCdnDataset(manifest_path)` in place of `load_dataset(streaming=True, ...)`. Tokenizer/collator remain unchanged.
- Add a Studio reuse guard before `.run()` to avoid redundant runs and quota use.

---

### 2. Correctness + actionability (resolved contradictions)
- **Recursive vs non-recursive listing:** Use non-recursive per-folder listing when generating the manifest (avoids huge pagination). The training loader only needs the flat file list in the manifest.
- **Streaming vs full download:** We stream per-file bytes over CDN (not full `datasets` streaming). This avoids API calls and schema inference while keeping memory bounded.
- **Schema handling:** Do not rely on HF `Features` or Arrow schema. Parse raw bytes as JSONL (preferred) or top-level JSON array and project only `prompt`/`response`. This prevents `pyarrow.CastError`.
- **Retries:** Exponential backoff for transient errors; specific 429 handling with longer waits (CDN limits are higher, but we wait 60s × attempt).
- **Multiprocessing:** Split files across workers by `torch.utils.data.get_worker_info()` to avoid duplicate work.

---

### 3. Implementation steps (ordered)

1. Generate manifest (run once on Mac)
   ```bash
   python tools/manifest_loader.py --repo datasets/my_repo --date 2026-05-01 --out file_manifest.json
   ```

2. Add `tools/manifest_loader.py` (see code below).

3. Add `surrogate/data/manifest_dataset.py` (see code below).

4. Update surrogate training script
   ```python
   # before
   # ds = load_dataset("json", data_files=..., streaming=True)

   # after
   from surrogate.data.manifest_dataset import ManifestCdnDataset
   ds = ManifestCdnDataset(manifest_path="file_manifest.json")
   ```

5. Studio reuse guard (add before `.run()`)
   ```python
   import os
   if os.environ.get("STUDIO_RUN_URL"):
       print("Reusing existing studio run; skipping duplicate .run()")
   else:
       run = wandb.init(...)  # or whatever launcher
   ```

---

### 4. Code (final, consolidated)

#### `tools/manifest_loader.py`
```python
#!/usr/bin/env python3
"""
Generate a CDN manifest for a Hugging Face dataset repo folder.
Usage:
  python tools/manifest_loader.py --repo datasets/my_repo --date 2026-05-01 --out file_manifest.json
"""
import argparse
import json
import time
from pathlib import Path
from typing import Dict, List

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_tree(api: HfApi, repo: str, date_folder: str) -> List[str]:
    """
    Non-recursive list for a date folder, then one-level deeper for subfolders.
    Returns repo-relative file paths.
    """
    files: List[str] = []
    try:
        items = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    except Exception as exc:
        raise RuntimeError(f"Failed to list {repo}/{date_folder}: {exc}") from exc

    for item in items:
        if item.type == "file":
            files.append(item.path)
        elif item.type == "dir":
            try:
                sub = api.list_repo_tree(repo=repo, path=item.path, recursive=False)
            except Exception:
                continue
            for s in sub:
                if s.type == "file":
                    files.append(s.path)
    return files

def build_manifest(repo: str, date_folder: str, out_path: Path) -> Dict:
    api = HfApi()
    files = list_date_tree(api, repo, date_folder)
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created": int(time.time()),
        "files": sorted(files),
    }
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path} ({len(files)} files)")
    return manifest

def cdn_url(manifest: Dict, repo_path: str) -> str:
    return CDN_TEMPLATE.format(repo=manifest["repo"], path=repo_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN manifest for HF dataset folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., datasets/my_repo)")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-05-01)")
    parser.add_argument("--out", default="file_manifest.json", help="Output manifest path")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, Path(args.out))
```

#### `surrogate/data/manifest_dataset.py`
```python
#!/usr/bin/env python3
"""
IterableDataset that reads files listed in a manifest via CDN URLs only.
Projects only {prompt, response} fields at parse time to avoid mixed-schema errors.
"""
import io
import json
import logging
import os
import time
from typing import Dict, Iterable, Optional

import torch
from torch.utils.data import IterableDataset
from tqdm import tqdm

logger = logging.getLogger(__name__)

class ManifestCdnDataset(IterableDataset):
    """
    Args:
        manifest_path: JSON manifest produced by tools/manifest_loader.py
        subset: optional list of file paths to limit to
        max_retries: max retry attempts per file
        retry_wait: initial wait in seconds (exponential backoff base)
    """

    def __init__(
        self,
        manifest_path: str,
        subset: Optional[Iterable[str]] = None,
        max_retries: int = 5,
        retry_wait: float = 1.0,
    ):
        super().__init__()
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = list(subset) if subset is not None else self.manifest["files"]
        self.max_retries = max_retries
        self.retry_wait = retry_wait

    @staticmethod
    def _fetch_cdn(url: str, timeout: int = 30) -> bytes:
        last_exc = None
        for attempt in range(1, 6):
            try:
                import requests
                resp = requests.get(url, timeout=timeout)
                if resp.status_code == 429:
                    wait = 60 * attempt
                    logger.warning(f"CDN 429 on {url}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.content
            except Exception as exc:
                last_exc = exc
                logger.warning(f"Retry {attempt}/5 for {url}: {exc}")
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Failed to fetch {url}") from last_exc

    @staticmethod
    def _project_record(raw: Dict) -> Dict[str, str]:
        """Keep only prompt/response; coerce to string."""
        prompt = raw.get("prompt") or raw.get("input") or raw.get("text") or ""
        response = raw.get("response") or raw.get("output") or raw.get("completion") or ""
        return {"prompt": str(prompt), "response": str(response)}

    def _stream_file(self, file_path: str
