# airship / discovery

Below is the **single, merged, production-ready implementation** that keeps every high-leverage idea from both candidates while resolving contradictions in favor of correctness, speed, and zero HF API calls during training.

---

## Final Implementation Plan (<2h ship)

1. **Eliminate HF API during training**  
   - Use CDN-only `IterableDataset` (no `datasets`, no auth, no 429s).
   - Pre-list parquet files once via a Mac/Linux script; embed the list in `file_list.json`.

2. **Correct schema/projection handling**  
   - Parse parquet with `pyarrow` (fast, low-memory).  
   - Project to `{prompt, response}` with robust column fallback.  
   - Avoid `pandas` inside hot loop (removed from Candidate 1 to reduce deps/overhead).

3. **Training reliability**  
   - `IterableDataset` + `DataLoader(num_workers=0)` for compatibility.  
   - Deterministic tokenization + proper `labels` copy.  
   - Graceful per-file error handling so one bad parquet doesn’t kill the run.

4. **Orchestration**  
   - Single `generate_file_list.py` script to snapshot CDN file list once.  
   - Supports `train`/`validation` splits; embeds row counts for monitoring.

---

## 1) CDN-only Iterable Dataset  
`surrogate/training/cdn_dataset.py`

```python
# surrogate/training/cdn_dataset.py
import json
import logging
from typing import Dict, Iterator, List
from dataclasses import dataclass

import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset
from tqdm import tqdm

logger = logging.getLogger(__name__)


@dataclass
class CdnIterableDataset(IterableDataset):
    """
    HF CDN-only dataset loader that bypasses HF API rate limits.
    Downloads parquet files directly from public CDN URLs.

    Usage:
        dataset = CdnIterableDataset(
            repo_id="org/surrogate-data",
            file_list_path="file_list.json",
            split="train"
        )
    """

    repo_id: str
    file_list_path: str
    split: str = "train"
    base_url: str = "https://huggingface.co/datasets"
    max_retries: int = 3
    retry_wait: float = 5.0
    columns: List[str] = None  # optional strict column selection

    def __post_init__(self) -> None:
        with open(self.file_list_path) as f:
            file_entries = json.load(f)

        if self.split not in file_entries:
            raise ValueError(
                f"Split '{self.split}' not in file_list.json. "
                f"Available: {list(file_entries.keys())}"
            )

        self.files: List[Dict] = file_entries[self.split]
        logger.info(
            f"CDN dataset init: split='{self.split}' files={len(self.files)}"
        )

    def _download_with_retry(self, url: str) -> bytes:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                return resp.content
            except requests.RequestException as exc:
                wait = self.retry_wait * (2 ** (attempt - 1))
                logger.warning(
                    f"Download attempt {attempt}/{self.max_retries} failed for {url}: {exc}. "
                    f"Retrying in {wait:.1f}s"
                )
                if attempt == self.max_retries:
                    raise
                import time

                time.sleep(wait)
        raise RuntimeError(f"Exhausted retries for {url}")

    @staticmethod
    def _normalize_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, float) and value != value:  # NaN
            return ""
        return str(value).strip()

    def _parse_parquet(self, data: bytes) -> Iterator[Dict[str, str]]:
        """
        Parse parquet bytes and project to {prompt, response}.
        Uses pyarrow for speed and schema robustness.
        """
        try:
            with pq.ParquetFile(pq.BufferReader(data)) as pf:
                # Read all row groups in one go (small/medium parquet files expected)
                table = pf.read()

            col_names = [c.lower() for c in table.column_names]

            # Resolve prompt/response columns
            prompt_idx = next((i for i, c in enumerate(col_names) if "prompt" in c), None)
            response_idx = next((i for i, c in enumerate(col_names) if "response" in c), None)

            # Fallback: first two large-string columns
            if prompt_idx is None or response_idx is None:
                str_cols = [
                    i
                    for i, c in enumerate(table.column_names)
                    if table.schema.types[i] in (pa.string(), pa.large_string())
                ]
                if len(str_cols) >= 2:
                    prompt_idx, response_idx = str_cols[0], str_cols[1]
                else:
                    # Last resort: first two columns
                    if table.num_columns >= 2:
                        prompt_idx, response_idx = 0, 1
                    else:
                        logger.error(
                            f"Cannot resolve prompt/response in columns: {table.column_names}"
                        )
                        return

            prompt_col = table.column(prompt_idx)
            response_col = table.column(response_idx)

            for i in range(table.num_rows):
                prompt = self._normalize_text(prompt_col[i].as_py())
                response = self._normalize_text(response_col[i].as_py())
                if not prompt and not response:
                    continue
                yield {"prompt": prompt, "response": response}

        except Exception as exc:
            logger.error(f"Failed to parse parquet: {exc}", exc_info=True)
            return

    def __iter__(self) -> Iterator[Dict[str, str]]:
        for entry in tqdm(self.files, desc=f"Loading {self.split} from CDN"):
            file_path = entry["file"]
            cdn_url = f"{self.base_url}/{self.repo_id}/resolve/main/{file_path}"
            try:
                data = self._download_with_retry(cdn_url)
                yield from self._parse_parquet(data)
            except Exception as exc:
                logger.error(f"Failed to load {file_path}: {exc}")
                continue
```

---

## 2) Updated Training Script  
`surrogate/training/train.py`

```python
# surrogate/training/train.py
import os
import logging
from pathlib import Path

import torch
from torch.utils.data import DataLoader
from transformers import AutoTokenizer, AutoModelForCausalLM, Trainer, TrainingArguments

from cdn_dataset import CdnIterableDataset

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---- CONFIG ----
REPO_ID = os.getenv("HF_DATASET_REPO", "org/surrogate-data")
FILE_LIST_PATH = Path("file_list.json")
OUTPUT_DIR = Path("./surrogate-checkpoints")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", 4))
GRADIENT_ACCUMULATION = int(os.getenv("GRADIENT_ACCUMULATION", 8))
MAX_STEPS = int(os.getenv("MAX_STEPS", 1000))

# ---- DATASET ----
train_dataset = CdnIterableDataset(
    repo_id=REPO_ID,
    file_list_path=str(FILE_LIST_PATH),
    split="train",
)

# ---- MODEL ----
model_name = os.getenv("MODEL_NAME", "Qwen/Qwen2.5-0.5B-Instruct")
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModelForCausalLM.from_pretrained(
    model_name,
    torch_dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16,
)

if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

# ---- COLLATE ----
def collate_fn(batch):
    texts = [
        f"User: {item['prompt']}\nAssistant: {item['response']}"
        for item in batch
    ]
    encodings = tokenizer(
        texts, truncation=True, padding=True, max_length=512
    )
    encodings["labels"] = [l[:] for l in encodings["input_ids"]]
    return {k: torch.tensor(v) for k, v in encodings.items()}

