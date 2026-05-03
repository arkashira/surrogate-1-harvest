# airship / frontend

Below is the single, consolidated implementation.  
It merges the strongest, non-overlapping parts of both proposals, removes contradictions, and keeps everything strictly actionable and correct.

---

## 1) Highest-value change (summary)
- **Eliminate HF API calls during training** by pre-listing files once on your Mac and embedding that list in training.
- **Fetch parquet exclusively via CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) so training never hits rate limits or requires tokens.
- **Make Lightning Studio idle-resilient** by reusing a running studio and auto-restarting it if stopped before `.run()`.

---

## 2) Files to create/modify
1. `/opt/axentx/airship/scripts/list_hf_files.py` — one-time file lister (run on Mac after rate-limit window).
2. `/opt/axentx/airship/surrogate/train.py` — Lightning entrypoint with CDN-only parquet loading and idle-resilient runner.

---

## 3) Implementation (120 min budget)

| Time | Task |
|------|------|
| 0–15 min | Create `scripts/list_hf_files.py` (uses `list_repo_tree`, saves JSON). |
| 15–45 min | Create `surrogate/train.py` (Lightning, CDN parquet streaming, no HF API). |
| 45–60 min | Add idle-resilient runner (reuse studio, restart on stop). |
| 60–75 min | Local dry-run with a small file list. |
| 75–90 min | Verify Lightning Studio launch and CDN fetch. |
| 90–120 min | Polish, comments, and usage docs. |

---

## 4) `/opt/axentx/airship/scripts/list_hf_files.py`

```python
#!/usr/bin/env python3
"""
List public HF dataset files (non-recursive) and save to JSON.
Run from your Mac after the rate-limit window clears.

Usage:
    python scripts/list_hf_files.py \
        --repo "datasets/my-org/surrogate-mirror" \
        --path "batches/mirror-merged/2026-05-03" \
        --out "surrogate/file_list_2026-05-03.json"
"""

import argparse
import json
import os
import sys
from typing import List

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

def list_hf_files(repo_id: str, path: str, out_path: str) -> List[str]:
    api = HfApi()
    # recursive=False keeps API calls minimal; call per subfolder if needed later
    tree = api.list_repo_tree(repo_id=repo_id, path=path, recursive=False)
    files = [
        item.rfilename
        for item in tree
        if not item.rfilename.endswith("/")
    ]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"repo_id": repo_id, "path": path, "files": files}, f, indent=2)
    print(f"Saved {len(files)} files to {out_path}")
    return files

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List HF dataset files (non-recursive).")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/owner/name)")
    parser.add_argument("--path", required=True, help="Folder path in repo")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()
    list_hf_files(args.repo, args.path, args.out)
```

Make executable:
```bash
chmod +x /opt/axentx/airship/scripts/list_hf_files.py
```

---

## 5) `/opt/axentx/airship/surrogate/train.py`

```python
#!/usr/bin/env python3
"""
Lightning training entrypoint for Surrogate AI.

Key behaviors:
- CDN-only parquet loading (zero HF API calls during training)
- Reuse running studio to save quota
- Idle-resilient: restart studio if stopped before .run()
- Minimal schema: expects parquet with {prompt,response} (others ignored)

Usage (local test):
    python train.py --file_list ../file_list_2026-05-03.json --max_steps 100

Usage (Lightning Studio):
    lightning run model train.py --cloud lightning-lambda-prod --machine L40S
"""

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import Dataset, DataLoader
import lightning as L

# ---- CDN Dataset (no HF API) ----
class CDNParquetDataset(Dataset):
    """
    Loads rows from parquet files via CDN URLs.
    File list JSON format: {"repo_id": "...", "path": "...", "files": [...]}
    Each file entry is repo-relative path (e.g. batches/mirror-merged/2026-05-03/part-0.parquet)
    """

    CDN_BASE = "https://huggingface.co/datasets"

    def __init__(self, file_list_path: str, max_rows: int = 10_000_000):
        super().__init__()
        with open(file_list_path) as f:
            meta = json.load(f)
        self.repo_id = meta["repo_id"]
        self.files = meta["files"]
        self.rows: List[Dict[str, str]] = []
        self._load_rows(max_rows)

    def _cdn_url(self, rfilename: str) -> str:
        # Public CDN; no Authorization header required
        return f"{self.CDN_BASE}/{self.repo_id}/resolve/main/{rfilename}"

    def _load_rows(self, max_rows: int) -> None:
        total = 0
        for fn in self.files:
            if total >= max_rows:
                break
            url = self._cdn_url(fn)
            try:
                # Use requests + pyarrow for robust CDN streaming
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                table = pq.read_table(pq.ParquetFile(pq.ParquetReader(pq.BufferReader(resp.content))))
                # Keep only expected columns if present
                cols = [c for c in ["prompt", "response"] if c in table.column_names]
                if not cols:
                    print(f"Skipping {fn}: missing prompt/response")
                    continue
                df = table.select(cols).to_pandas()
                # Normalize missing values
                for col in cols:
                    if col not in df.columns:
                        df[col] = ""
            except Exception as exc:
                print(f"Skipping {fn}: {exc}")
                continue

            for _, row in df.iterrows():
                self.rows.append({
                    "prompt": str(row.get("prompt", "")),
                    "response": str(row.get("response", "")),
                })
                total += 1
                if total >= max_rows:
                    break
        print(f"Loaded {len(self.rows)} rows from {len(self.files)} files")

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, idx: int) -> Dict[str, str]:
        return self.rows[idx]

# ---- Tokenizer stub / collate ----
def dummy_tokenize(batch: List[Dict[str, str]]) -> Dict[str, torch.Tensor]:
    """
    Replace with real tokenizer (e.g. Qwen/Mistral tokenizer).
    Returns dummy tensors so Lightning can run.
    """
    bs = len(batch)
    return {
        "input_ids": torch.randint(0, 32000, (bs, 512), dtype=torch.long),
        "labels": torch.randint(0, 32000, (bs, 512), dtype=torch.long),
    }

def collate_fn(batch: List[Dict[str, str]]) -> Dict[str, torch.Tensor]:
    return dummy_tokenize(batch)

# ---- Surrogate LitModule ----
class SurrogateModule(L.LightningModule):
    def __init__(self, lr: float = 
