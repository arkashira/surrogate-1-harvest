# airship / frontend

## Final Implementation (synthesized)

**Highest-value change**: Add a CDN-only parquet loader and Lightning idle-resilient runner to `/opt/axentx/airship/surrogate/train.py` (create/update) plus `scripts/list_hf_files.py`. This eliminates HF API rate limits during training and prevents quota loss from idle timeouts.

### Key decisions (correctness + actionability)
- Use **non-recursive per-folder listing** to avoid pagination explosion and keep the HF API call minimal.
- Use **CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) — no Authorization header, bypasses `/api/` rate limits.
- **Project only `{prompt,response}`** with best-effort fallback to common aliases; fail fast if neither exists.
- **Lightning idle-resilience**: checkpointing + automatic resume via `Trainer` from the latest checkpoint; Studio reuse if available.
- **Deterministic, reproducible runs**: seed everything and use explicit `max_files`/`max_steps` caps for fast iteration.
- **Executable scripts with proper shebangs** and `chmod +x` for direct invocation.

---

### 1) scripts/list_hf_files.py

```bash
#!/usr/bin/env bash
# Wrapper: list_hf_files.sh <repo> <date_folder> <out_json>
set -euo pipefail
exec python3 "$(dirname "$0")/../airship/surrogate/scripts/list_hf_files.py" "$@"
```

```python
# /opt/axentx/airship/surrogate/scripts/list_hf_files.py
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: list_hf_files.py <repo> <date_folder> <out_json>")
        sys.exit(1)

    repo, date_folder, out_json = sys.argv[1], sys.argv[2], sys.argv[3]
    api = HfApi()

    # Non-recursive per-folder listing to avoid pagination explosion
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = sorted(
        f.rfilename
        for f in tree
        if f.rfilename.lower().endswith((".parquet", ".parq"))
    )

    # CDN URLs (no auth, bypasses /api/ rate limits)
    entries = [
        {
            "repo": repo,
            "path": f,
            "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{f}"
        }
        for f in files
    ]

    os.makedirs(os.path.dirname(os.path.abspath(out_json)), exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as fp:
        json.dump(entries, fp, indent=2, ensure_ascii=False)

    print(f"Wrote {len(entries)} parquet file entries to {out_json}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x /opt/axentx/airship/surrogate/scripts/list_hf_files.py
chmod +x /opt/axentx/airship/surrogate/scripts/list_hf_files.sh 2>/dev/null || true
```

---

### 2) /opt/axentx/airship/surrogate/train.py

```python
#!/usr/bin/env python3
"""
CDN-only parquet loader + Lightning idle-resilient training runner.

Usage:
  python train.py --files-list scripts/file_list.json --output-model ./ckpt

Notes:
- Uses HuggingFace CDN URLs (no Authorization header) to bypass API rate limits.
- Resumes automatically from latest checkpoint to survive idle stops.
- Projects only {prompt, response} fields; attribution via filename pattern.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import Dataset, DataLoader
from lightning.pytorch import LightningModule, Trainer, seed_everything, Callback
from lightning.pytorch.callbacks import ModelCheckpoint

try:
    from lightning.pytorch import Studio
except ImportError:
    Studio = None

# ----------
# Dataset
# ----------
class CDNParquetDataset(Dataset):
    """Stream parquet files from CDN URLs; project to {prompt, response} only."""

    def __init__(self, entries: List[Dict], max_files: Optional[int] = None):
        self.entries = entries[:max_files] if max_files else entries
        self._cache: Dict[str, pa.Table] = {}

    def _load_table(self, entry: Dict) -> pa.Table:
        url = entry["cdn_url"]
        if url in self._cache:
            return self._cache[url]

        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        table = pq.read_table(pa.BufferReader(resp.content))

        cols = table.column_names
        has_prompt = "prompt" in cols
        has_response = "response" in cols

        if not has_prompt or not has_response:
            prompt_col = next((c for c in cols if "prompt" in c.lower()), None)
            response_col = next((c for c in cols if "response" in c.lower()), None)
            if prompt_col and response_col:
                table = table.select([prompt_col, response_col]).rename_columns(["prompt", "response"])
            else:
                raise ValueError(f"Missing prompt/response in {url}; columns: {cols}")
        else:
            table = table.select(["prompt", "response"])

        self._cache[url] = table
        return table

    def __len__(self) -> int:
        return len(self.entries)

    def __getitem__(self, idx: int) -> Dict[str, str]:
        entry = self.entries[idx]
        table = self._load_table(entry)
        # Deterministic single-row sample per file for minimal loader.
        # For production, stream row groups or shard files across workers.
        row_idx = 0
        prompt = str(table["prompt"][row_idx].as_py())
        response = str(table["response"][row_idx].as_py())
        return {"prompt": prompt, "response": response}

# ----------
# Lightning Module (minimal)
# ----------
class SurrogateLM(LightningModule):
    def __init__(self, vocab_size: int = 50257, d_model: int = 768, lr: float = 1e-4):
        super().__init__()
        self.save_hyperparameters()
        self.lr = lr
        self.token_emb = torch.nn.Embedding(vocab_size, d_model)
        self.lm_head = torch.nn.Linear(d_model, vocab_size)
        self.loss_fn = torch.nn.CrossEntropyLoss()

    def forward(self, x):
        return self.lm_head(self.token_emb(x))

    def training_step(self, batch, batch_idx):
        prompt = batch["prompt"]
        # Mock token ids: hash-based deterministic ids for demo
        ids = torch.tensor([hash(p) % self.hparams.vocab_size], dtype=torch.long).unsqueeze(0).to(self.device)
        target = ids.clone()
        logits = self(ids)
        loss = self.loss_fn(logits.view(-1, self.hparams.vocab_size), target.view(-1))
        self.log("train_loss", loss, prog_bar=True)
        return loss

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), lr=self.lr)

# ----------
# Utilities
# ----------
def load_file_list(path: str) -> List[Dict]:
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)
    if not entries:
        raise ValueError("No entries in file list")
    for e in entries:
        if "cdn_url" not in e:
            raise ValueError("Each entry must contain 'cdn_url'")
    return entries

def ensure_studio_running(name: str) -> Optional["Studio"]:
    if Studio is None:
        print("Lightning Studio not available (install lightning[studio] extras). Running locally.")
        return None

    teamspace = Studio.teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running Studio: {name}")
            return s

    print(f"No running Studio '{name}' found. Create/start one manually or run
