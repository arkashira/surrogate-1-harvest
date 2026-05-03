# airship / frontend

## Implementation Plan (≤2h)

**Highest-value change**: Add CDN-only dataset loader + Lightning idle-resilient runner to `/opt/axentx/airship/surrogate/train.py` (create if missing) and a companion `scripts/list_hf_files.py` so training never hits HF API during data loading and survives Lightning idle stops.

### Concrete steps
1. Create `scripts/list_hf_files.py` — Mac-side script to list one date folder via `list_repo_tree`, save to JSON (single API call, respects 429/1000).
2. Create `surrogate/train.py` — Lightning Studio entrypoint that:
   - Loads file list from JSON (embedded or mounted).
   - Streams parquet via HF CDN URLs (`resolve/main/...`) with zero auth/zero API calls.
   - Projects to `{prompt, response}` only.
   - Checks Studio status before each run; auto-restarts on idle stop.
3. Add `requirements.txt` line for `lightning` and `pyarrow` if missing.
4. Make scripts executable and ensure Bash shebang + `SHELL=/bin/bash` for cron safety.

---

## 1) `scripts/list_hf_files.py` (run from Mac)

```python
#!/usr/bin/env python3
"""
List one date folder in a HF dataset repo via list_repo_tree (non-recursive),
save to JSON for CDN-only training.

Usage (Mac):
    SHELL=/bin/bash
    python scripts/list_hf_files.py \
        --repo "axentx/surrogate-data" \
        --date "2026-04-29" \
        --out "data/file_list_2026-04-29.json"
"""
import argparse
import json
import os
import time
from pathlib import Path

import huggingface_hub

HF_TOKEN = os.getenv("HF_TOKEN")

def list_date_folder(repo: str, date: str, out_path: Path):
    client = huggingface_hub.HfApi(token=HF_TOKEN)

    # Single API call: non-recursive tree for the date folder
    # Avoids recursive pagination and 429 on big repos
    try:
        tree = client.list_repo_tree(
            repo=repo,
            path=date,
            recursive=False,
        )
    except huggingface_hub.utils.HfHubHTTPError as e:
        if e.response.status_code == 429:
            print("Rate limited (429). Wait 360s before retry.")
            time.sleep(360)
            return list_date_folder(repo, date, out_path)
        raise

    files = [
        {"path": f.path, "size": getattr(f, "size", None)}
        for f in tree
        if f.path.lower().endswith(".parquet")
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"repo": repo, "date": date, "files": files}, f, indent=2)

    print(f"Saved {len(files)} parquet files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="HF dataset repo")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    list_date_folder(args.repo, args.date, Path(args.out))
```

Make executable:
```bash
chmod +x scripts/list_hf_files.py
```

Cron-safe invocation (if scheduled):
```bash
SHELL=/bin/bash
bash scripts/list_hf_files.py --repo "axentx/surrogate-data" --date "2026-04-29" --out "data/file_list_2026-04-29.json"
```

---

## 2) `surrogate/train.py` (Lightning Studio entrypoint)

```python
#!/usr/bin/env python3
"""
Lightning Studio training script:
- CDN-only parquet loading (no HF API/auth during data load)
- Projects to {prompt, response}
- Survives Lightning idle stop by checking status and restarting machine
"""
import json
import os
import time
from pathlib import Path

import lightning as L
import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import IterableDataset, DataLoader

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-data")
FILE_LIST_JSON = os.getenv("FILE_LIST_JSON", "data/file_list_2026-04-29.json")
MACHINE = os.getenv("LIGHTNING_MACHINE", "lightning-lambda-prod/L40S")  # fallback handled below

# ---------- CDN-only IterableDataset ----------
class CDNParquetIterable(IterableDataset):
    def __init__(self, file_list):
        self.file_urls = [
            f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main/{f['path']}"
            for f in file_list
        ]

    def __iter__(self):
        for url in self.file_urls:
            # Stream from CDN (no auth, no HF API)
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                local_path = "/tmp/current.parquet"
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

            table = pq.read_table(local_path, columns=["prompt", "response"])
            df = table.to_pandas()
            for _, row in df.iterrows():
                # Project to {prompt, response} only
                yield {
                    "prompt": str(row["prompt"]),
                    "response": str(row["response"]),
                }
            os.remove(local_path)

# ---------- Lightning Module (minimal) ----------
class SurrogateTrainer(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.model = torch.nn.Linear(1, 1)  # placeholder; replace with real model

    def training_step(self, batch, batch_idx):
        # Replace with real forward/loss
        loss = torch.tensor(0.0, requires_grad=True)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)

# ---------- Idle-resilient runner ----------
def ensure_studio_running(studio_name: str):
    """
    Check Studio status and restart if stopped (idle timeout kills training).
    """
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == studio_name and s.status == "running":
            print(f"Reusing running Studio: {studio_name}")
            return s

    print(f"Starting Studio: {studio_name}")
    # Prefer L40S in lightning-lambda-prod (H200 not in free clouds)
    machine = L.Machine.from_string(MACHINE)
    return L.Studio(
        name=studio_name,
        machine=machine,
        create_ok=True,
    )

def main():
    # Load file list (embedded by list_hf_files.py)
    file_list_path = Path(FILE_LIST_JSON)
    if not file_list_path.exists():
        raise FileNotFoundError(f"File list not found: {file_list_path}")

    with open(file_list_path) as f:
        file_list = json.load(f)["files"]

    dataset = CDNParquetIterable(file_list)
    loader = DataLoader(dataset, batch_size=8)

    # Studio reuse + idle-resilience
    studio = ensure_studio_running("surrogate-train-studio")
    if studio.status != "running":
        studio.start(machine=L.Machine.from_string(MACHINE))

    model = SurrogateTrainer()
    trainer = L.Trainer(max_epochs=1, devices=1, accelerator="gpu")
    trainer.fit(model, loader)

if __name__ == "__main__":
    main()
```

---

## 3) Requirements (add if missing)

`surrogate/requirements.txt` (or project-level):
```
lightning>=2.2
pyarrow
requests
```

---

## 4) Execution checklist (≤2h)

- [ ] `
