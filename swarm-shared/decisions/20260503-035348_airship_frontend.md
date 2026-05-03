# airship / frontend

## Implementation Plan (≤2h)

**Highest-value change**: Add CDN-only dataset loader + Lightning idle-resilient runner to `/opt/axentx/airship/surrogate/train.py` (create if missing) and a companion `scripts/list_hf_files.py` so training never hits HF API during data loading and survives Lightning idle stops.

### Concrete steps
1. Create `scripts/list_hf_files.py` — run once from Mac after rate-limit window to list a single date folder and emit `file_list.json`.
2. Create/update `surrogate/train.py` — reads `file_list.json`, downloads via CDN URLs only, projects to `{prompt,response}`, handles Lightning idle-stop by checking status and restarting on L40S.
3. Ensure scripts are executable and use proper shebangs.

---

## 1. `scripts/list_hf_files.py`

```python
#!/usr/bin/env python3
"""
Run once (Mac) after HF API rate-limit clears.
Lists ONE date folder (non-recursive) and saves file list to JSON.
Usage:
    python scripts/list_hf_files.py \
        --repo datasets/your-org/surrogate-mirror \
        --date 2026-04-29 \
        --out surrogate/file_list.json
"""

import argparse
import json
import os
import sys

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="HF dataset repo, e.g. datasets/your-org/surrogate-mirror")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-04-29")
    parser.add_argument("--out", default="surrogate/file_list.json", help="Output JSON path")
    args = parser.parse_args()

    api = HfApi()
    # List one folder only (non-recursive) to avoid pagination explosion
    folder = f"batches/mirror-merged/{args.date}"
    entries = api.list_repo_tree(repo_id=args.repo, path=folder, recursive=False)

    files = []
    for e in entries:
        if e.type != "file":
            continue
        # CDN URL (no Authorization header)
        cdn_url = f"https://huggingface.co/datasets/{args.repo}/resolve/main/{e.path}"
        files.append(
            {
                "path": e.path,
                "cdn_url": cdn_url,
                "size": getattr(e, "size", None),
            }
        )

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"repo": args.repo, "date": args.date, "folder": folder, "files": files}, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x scripts/list_hf_files.py
```

---

## 2. `surrogate/train.py`

```python
#!/usr/bin/env python3
"""
CDN-only dataset loader + Lightning idle-resilient runner.
- Reads surrogate/file_list.json
- Downloads via CDN URLs (bypasses HF API auth/rate-limit)
- Projects to {prompt, response} only
- Survives Lightning idle-stop by restarting on L40S

Usage (Lightning Studio or local):
    python surrogate/train.py --file-list surrogate/file_list.json
"""

import argparse
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Iterator

import pyarrow as pa
import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import IterableDataset, DataLoader

try:
    from lightning import Fabric, LightningModule, Trainer
    from lightning.fabric.plugins import LightningEnvironment
    from lightning.pytorch.strategies import DDPStrategy
except ImportError:
    print("Install lightning: pip install lightning")
    sys.exit(1)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("surrogate.train")

# ---------- CDN dataset ----------
class CDNParquetDataset(IterableDataset):
    def __init__(self, file_list_path: str, max_retries: int = 3):
        super().__init__()
        with open(file_list_path) as f:
            manifest = json.load(f)
        self.files: List[Dict] = manifest["files"]
        self.max_retries = max_retries

    def _download_parquet(self, cdn_url: str) -> bytes:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(cdn_url, timeout=30)
                resp.raise_for_status()
                return resp.content
            except Exception as exc:
                log.warning("Download failed %s (attempt %s/%s): %s", cdn_url, attempt, self.max_retries, exc)
                if attempt == self.max_retries:
                    raise
        raise RuntimeError(f"Failed to download {cdn_url}")

    def _project_to_prompt_response(self, table: pa.Table) -> Iterator[Dict[str, str]]:
        # Expect columns that can be mapped to prompt/response; adapt as needed.
        # Common patterns: prompt/response, instruction/output, question/answer
        col_names = [c.lower() for c in table.column_names]

        prompt_col = None
        response_col = None

        for pc in ("prompt", "instruction", "question", "input"):
            if pc in col_names:
                prompt_col = table.column_names[col_names.index(pc)]
                break
        for rc in ("response", "output", "answer", "completion"):
            if rc in col_names:
                response_col = table.column_names[col_names.index(rc)]
                break

        if prompt_col is None or response_col is None:
            # Fallback: first two text columns
            text_cols = [c for c in table.column_names if pa.types.is_string(table.schema.field(c).type)]
            if len(text_cols) >= 2:
                prompt_col, response_col = text_cols[0], text_cols[1]
            else:
                raise ValueError(f"Cannot map prompt/response in {table.column_names}")

        for i in range(table.num_rows):
            yield {
                "prompt": str(table.column(prompt_col)[i].as_py()),
                "response": str(table.column(response_col)[i].as_py()),
            }

    def __iter__(self) -> Iterator[Dict[str, str]]:
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            iter_files = self.files
        else:
            per_worker = len(self.files) // worker_info.num_workers
            worker_id = worker_info.id
            iter_files = self.files[worker_id * per_worker : (worker_id + 1) * per_worker]

        for entry in iter_files:
            cdn_url = entry["cdn_url"]
            try:
                raw = self._download_parquet(cdn_url)
                with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
                    tmp.write(raw)
                    tmp_path = tmp.name
                table = pq.read_table(tmp_path)
                os.unlink(tmp_path)
                yield from self._project_to_prompt_response(table)
            except Exception as exc:
                log.error("Failed to process %s: %s", cdn_url, exc)
                continue

# ---------- Dummy model for demo ----------
class SurrogateLM(LightningModule):
    def __init__(self, vocab_size: int = 32000, d_model: int = 768):
        super().__init__()
        self.save_hyperparameters()
        self.embed = torch.nn.Embedding(vocab_size, d_model)
        self.lm_head = torch.nn.Linear(d_model, vocab_size)
        self.loss_fn = torch.nn.CrossEntropyLoss()

    def training_step(self, batch, batch_idx):
        # batch: {"prompt":..., "response":...} — tokenize externally in collate
        input_ids = batch["input_ids"]
        labels = batch["labels"]
        logits = self.lm_head(self.embed(input_ids))
        loss = self.loss_fn(logits.view(-1, logits.size(-1)), labels.view(-1))
        self.log("train_loss", loss, prog_bar=True)
        return loss

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), lr=1e-4)

# ---------- Collate (simple tokenizer stub
