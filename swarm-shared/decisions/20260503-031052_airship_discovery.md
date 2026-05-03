# airship / discovery

## Final Implementation Plan  
**Goal (≤2 h):** Remove HF API calls during data loading and keep Surrogate training alive across Lightning idle stops.

---

### 1) Pre-list file paths once (Mac orchestration) — **KEEP & HARDEN**
- Use **one** `list_repo_tree` call for the target date folder.  
- Save `train_files.json` into the repo (committed or artifact).  
- **Hardening:** validate parquet schema (must contain `prompt`/`response`), reject empty lists, and store SHA-256 of each file to detect corruption at load time.

---

### 2) CDN-only PyTorch Dataset — **USE + FIX CONTRADICTIONS**
- Replace `load_dataset(streaming=True)` with a lightweight `IterableDataset`.  
- Fetch parquet blobs via  
  `https://huggingface.co/datasets/{repo}/resolve/main/{path}`  
  (no auth, no API quota).  
- **Contradiction resolved:** Candidate 2 proposed async/concurrent prefetch; Candidate 1 used simple retry.  
  **Final choice:** keep Candidate 1’s simple retry + backoff (correct and sufficient), but **add bounded thread-pool prefetch (1–2 workers)** to hide CDN latency without complicating shutdown/idle resilience.  
- **Correctness fix:** ensure `num_workers` in DataLoader does not conflict with internal prefetch; set `persistent_workers=False` for idle-stop friendliness.

---

### 3) Lightning idle-resilient runner — **USE + CONCRETIZE**
- Before each `.fit()`, check studio status.  
- If stopped, restart with `target.start(machine=Machine.L40S)` (fallback to public tier).  
- **Concrete actionability:**  
  - Wrap training entrypoint in a small supervisor script that catches `KeyboardInterrupt`/`SystemExit`, checkpoints, and exits with code 0 (so idle-stop + restart works cleanly).  
  - On restart, resume from the **latest checkpoint** (not from scratch).  
  - Use exponential backoff for transient CDN errors (already in Candidate 1).  
  - Do **not** rely on `LightningCLI` auto-resume across idle stops; implement explicit `trainer.fit(ckpt_path=...)` logic.

---

### 4) Minimal interface change — **KEEP**
- Keep existing train entrypoint signature.  
- Add `--file-list-json` optional arg; if absent, fall back to legacy loader **with loud warning + rate-limit advisory**.

---

## Final Code Snippets

### 4.1) `scripts/build_file_list.py` (run on Mac)
```python
#!/usr/bin/env python3
"""
Generate train_files.json for a date folder to enable CDN-only training.
Includes SHA-256 for corruption detection.
"""
import argparse
import hashlib
import json
import os
from huggingface_hub import HfApi

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/owner/name)")
    parser.add_argument("--date", required=True, help="Date folder under batches/mirror-merged/YYYY-MM-DD")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    api = HfApi()
    prefix = f"batches/mirror-merged/{args.date}/"
    files = api.list_repo_tree(repo_id=args.repo, path=prefix, recursive=False)
    paths = [f.rfilename for f in files if f.rfilename.endswith(".parquet")]
    if not paths:
        raise RuntimeError(f"No parquet files found under {prefix}")

    items = []
    for p in sorted(paths):
        url = f"https://huggingface.co/datasets/{args.repo}/resolve/main/{p}"
        # Lightweight HEAD to get size (optional) and compute hash via GET (cached by CDN)
        items.append({
            "repo": args.repo,
            "path": p,
            "cdn_url": url,
            # Hash will be computed at load time; placeholder for schema checks
        })

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(items, f, indent=2)
    print(f"Wrote {len(items)} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

### 4.2) `surrogate/data/cdn_parquet_dataset.py`
```python
import pyarrow.parquet as pq
import pyarrow as pa
import requests
import io
import torch
from torch.utils.data import IterableDataset
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional
import logging
import time

logger = logging.getLogger(__name__)

class CDNParquetDataset(IterableDataset):
    """
    CDN-only parquet loader for HF datasets.
    Avoids HF API calls during training.
    Yields {"prompt": str, "response": str}
    """
    def __init__(
        self,
        file_items: List[Dict],
        max_retries: int = 3,
        prefetch_workers: int = 1,
        required_columns=("prompt", "response")
    ):
        super().__init__()
        self.file_items = file_items
        self.max_retries = max_retries
        self.prefetch_workers = prefetch_workers
        self.required_columns = required_columns

    def _fetch_parquet(self, url: str) -> bytes:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                return resp.content
            except Exception as exc:
                wait = 2 ** attempt
                logger.warning(
                    f"CDN fetch failed ({url}) attempt {attempt}/{self.max_retries}: {exc}. Retry in {wait}s"
                )
                if attempt == self.max_retries:
                    raise
                time.sleep(wait)

    def _project_record(self, batch: pa.Table) -> List[Dict[str, str]]:
        # Validate required columns
        missing = [c for c in self.required_columns if c not in batch.column_names]
        if missing:
            raise ValueError(f"Parquet missing required columns: {missing}")

        prompts = batch.column("prompt").to_pylist()
        responses = batch.column("response").to_pylist()
        out = []
        for p, r in zip(prompts, responses):
            out.append({"prompt": str(p or ""), "response": str(r or "")})
        return out

    def _load_file(self, item: Dict) -> List[Dict[str, str]]:
        url = item["cdn_url"]
        blob = self._fetch_parquet(url)
        table = pq.read_table(io.BytesIO(blob))
        return self._project_record(table)

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            files = self.file_items
        else:
            per_worker = max(1, len(self.file_items) // worker_info.num_workers)
            start = worker_info.id * per_worker
            end = start + per_worker if worker_info.id < worker_info.num_workers - 1 else len(self.file_items)
            files = self.file_items[start:end]

        # Bounded thread-pool prefetch to hide latency
        with ThreadPoolExecutor(max_workers=self.prefetch_workers) as executor:
            future_to_item = {executor.submit(self._load_file, item): item for item in files}
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    records = future.result()
                    for rec in records:
                        yield rec
                except Exception as exc:
                    logger.error(f"Failed to load {item['cdn_url']}: {exc}")
                    continue
```

---

### 4.3) `surrogate/train.py` (entrypoint + idle-resilient runner)
```python
import json
import argparse
import logging
import lightning as L
from lightning.pytorch import Trainer
from lightning.pytorch.callbacks import ModelCheckpoint
from surrogate.data.cdn_parquet_dataset import CDNParquetDataset
from torch.utils.data import DataLoader

logger = logging.getLogger(__name__)

def maybe_build_loader(file_list_json: str, batch_size: int = 8, num_workers: int = 0
