# airship / discovery

### Final Integrated Implementation  
**Goal:** Eliminate HF API rate limits (429) and `pyarrow.CastError` on mixed schemas for Surrogate-1 training using a manifest-driven, CDN-only loader.  
**Estimated effort:** <2h  
**Value:** 24/7 autonomous training with deterministic shards and no Hugging Face API dependency during training.

---

### Concrete Action Plan (Resolved for Correctness + Actionability)

1. **Generate manifest once per date folder** (run on dev machine or CI after rate-limit window)  
   - Use `list_repo_tree(repo, path=folder, recursive=False)` to list parquet files.  
   - Emit `manifest-{date}.json` containing: `repo`, `date`, `files[]`.  
   - Commit or ship alongside training code.

2. **Add CDN-only dataset loader** (`surrogate/training/data/cdn_dataset.py`)  
   - Accepts local manifest; builds CDN URLs:  
     `https://huggingface.co/datasets/{repo}/resolve/main/{path}`  
   - Downloads via streaming HTTP (no auth, no API calls).  
   - Projects only `{prompt, response}` at parse time to prevent `pyarrow.CastError`.  
   - Includes retry/backoff for CDN transient errors.

3. **Replace training data loader**  
   - Swap `load_dataset(streaming=True, repo)` for `CdnDataset(manifest_path)`.  
   - Ensure deterministic shard order; add optional shuffle buffer over streamed examples.

4. **Smoke test**  
   - Run one training step locally or in Lightning Studio.  
   - Confirm zero HF API calls (no `huggingface_hub` requests) and correct tensor shapes.

---

### Final Code (Merged + Corrected)

#### 1) Manifest generator (run once)

`scripts/generate_cdn_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate manifest-{date}.json for CDN-only dataset loading.
Usage:
    HF_REPO=org/dataset python scripts/generate_cdn_manifest.py 2026-04-29
"""
import json
import os
import sys
from pathlib import Path

from huggingface_hub import list_repo_tree


def main(date: str) -> None:
    repo = os.environ.get("HF_REPO")
    if not repo:
        raise RuntimeError("Set HF_REPO=org/dataset")

    folder = f"batches/mirror-merged/{date}"
    entries = list_repo_tree(repo, path=folder, recursive=False)
    files = sorted(e.path for e in entries if e.path.endswith(".parquet"))

    manifest = {
        "repo": repo,
        "date": date,
        "files": files,
    }

    out_dir = Path("surrogate/training/data")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"manifest-{date}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python generate_cdn_manifest.py YYYY-MM-DD")
        sys.exit(1)
    main(sys.argv[1])
```

Run:

```bash
HF_REPO=org/surrogate-dataset python scripts/generate_cdn_manifest.py 2026-04-29
```

---

#### 2) CDN-only dataset loader

`surrogate/training/data/cdn_dataset.py`

```python
import io
import json
import random
import time
from pathlib import Path
from typing import Dict, Iterator

import httpx
import pyarrow.parquet as pq

HF_CDN = "https://huggingface.co/datasets"


class CdnDataset:
    """
    Manifest-driven, CDN-only dataset loader.
    - Avoids HF API entirely (no 429s).
    - Projects only {prompt, response} to prevent pyarrow.CastError.
    """

    def __init__(self, manifest_path: str, repo: str | None = None, max_retries: int = 3):
        manifest = json.loads(Path(manifest_path).read_text())
        self.repo = repo or manifest["repo"]
        self.files = manifest["files"]
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=60.0, follow_redirects=True)

    def _cdn_url(self, path: str) -> str:
        return f"{HF_CDN}/{self.repo}/resolve/main/{path}"

    def _stream_parquet(self, url: str) -> bytes:
        for attempt in range(self.max_retries):
            try:
                with self._client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    return b"".join(resp.iter_bytes(chunk_size=8192))
            except Exception:
                if attempt == self.max_retries - 1:
                    raise
                sleep = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(sleep)

    def __iter__(self) -> Iterator[Dict[str, str]]:
        for path in self.files:
            raw = self._stream_parquet(self._cdn_url(path))
            table = pq.read_table(io.BytesIO(raw))
            if {"prompt", "response"}.issubset(table.column_names):
                df = table.select(["prompt", "response"]).to_pandas()
                for _, row in df.iterrows():
                    yield {"prompt": row["prompt"], "response": row["response"]}

    def __len__(self) -> int:
        return len(self.files)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
```

---

#### 3) Training entrypoint patch

`surrogate/training/train.py` (minimal change)

```python
from torch.utils.data import IterableDataset
from data.cdn_dataset import CdnDataset

class IterableCdnDataset(IterableDataset):
    def __init__(self, manifest_path: str):
        self.manifest_path = manifest_path

    def __iter__(self):
        return CdnDataset(self.manifest_path)


def build_dataloader(manifest_path: str, batch_size: int = 8, shuffle_buffer: int = 1000):
    dataset = IterableCdnDataset(manifest_path)
    # Use DataLoader with num_workers=0 for simple streaming;
    # add shuffle via buffer if needed.
    from torch.utils.data import DataLoader
    return DataLoader(dataset, batch_size=batch_size, num_workers=0)
```

---

### Smoke Test Checklist
- [ ] Run one training step with `CdnDataset`.  
- [ ] Verify logs show no `huggingface_hub` requests.  
- [ ] Confirm shapes: `prompt` and `response` are present and correct.  
- [ ] Confirm no `pyarrow.CastError`.
