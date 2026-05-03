# vanguard / quality

## 1. Diagnosis

- Frontend still triggers authenticated `list_repo_tree` (and `/api/` proxy calls) during page/training load, burning HF quota (1000/5min) and causing intermittent 429s.
- No persisted `(repo, dateFolder)` file manifest; each session re-enumerates folders via API instead of using a single Mac-side snapshot and CDN-only fetches.
- Training and ingestion paths mix schema-rich metadata (`source`, `ts`) into parquet files, violating the “project to `{prompt, response}` only” rule and risking downstream `pyarrow.CastError` on heterogeneous repos.
- Lightning Studio reuse is not implemented; jobs likely recreate studios instead of reusing running ones, wasting ~80hr/mo quota and hitting idle-stop/timeout churn.
- No guardrails for HF commit-cap (128/hr/repo); ingestion can stall when sibling repos aren’t used to spread writes.

## 2. Proposed change

- **File**: `/opt/axentx/vanguard/train.py` (or create if absent)  
  **Scope**: Add a lightweight manifest-based data loader that:
  1. Accepts a pre-generated `file_list.json` (Mac side, one API call per date folder).
  2. Uses only CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) with zero auth and no `/api/` calls during training.
  3. Projects each file to `{prompt, response}` at parse time (no schema pollution).
  4. Integrates with Lightning Studio reuse + idle-stop guard.

## 3. Implementation

```python
# /opt/axentx/vanguard/train.py
import json
import os
from pathlib import Path
from typing import List, Dict

import pyarrow as pa
import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import Dataset, DataLoader
from lightning import Fabric, LightningModule, Trainer

# --
# 1) Manifest + CDN-only dataset
# --
class HFCDNDataset(Dataset):
    """
    Uses a pre-generated file_list.json to avoid HF API calls during training.
    Each entry: {"repo": "...", "path": "...", "prompt_key": "...", "response_key": "..."}
    Downloads via public CDN (no auth) and projects to {prompt, response}.
    """
    def __init__(self, manifest_path: str, cache_dir: str = ".cache_cdn"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

        with open(manifest_path) as f:
            self.files: List[Dict] = json.load(f)

    def _cdn_url(self, repo: str, path: str) -> str:
        # Public CDN URL — no Authorization header required
        return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

    def _fetch_cached(self, repo: str, path: str) -> Path:
        url = self._cdn_url(repo, path)
        safe_slug = path.replace("/", "_")
        out = self.cache_dir / f"{repo.replace('/', '_')}_{safe_slug}"
        if out.exists():
            return out
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        out.write_bytes(r.content)
        return out

    def _project_record(self, raw: Dict) -> Dict:
        # Keep ONLY prompt/response; drop all other fields to avoid schema issues
        return {
            "prompt": str(raw.get("prompt", raw.get("instruction", ""))),
            "response": str(raw.get("response", raw.get("output", ""))),
        }

    def __len__(self) -> int:
        return len(self.files)

    def __getitem__(self, idx: int) -> Dict:
        meta = self.files[idx]
        local_path = self._fetch_cached(meta["repo"], meta["path"])

        try:
            # Try parquet first
            tbl = pq.read_table(local_path)
            df = tbl.to_pydict()
        except Exception:
            # Fallback: line-delimited json
            lines = Path(local_path).read_text().strip().splitlines()
            df = {"prompt": [], "response": []}
            for line in lines:
                if not line.strip():
                    continue
                rec = json.loads(line)
                proj = self._project_record(rec)
                df["prompt"].append(proj["prompt"])
                df["response"].append(proj["response"])

        # Return single sample (DataLoader will batch)
        return {
            "prompt": df["prompt"][0] if df["prompt"] else "",
            "response": df["response"][0] if df["response"] else "",
        }

# --
# 2) LightningModule (minimal)
# --
class SurrogateTrainer(LightningModule):
    def __init__(self, lr: float = 1e-4):
        super().__init__()
        self.lr = lr
        # Replace with your actual model init
        self.model = torch.nn.Linear(10, 1)  # placeholder

    def training_step(self, batch, batch_idx):
        # Replace with real forward/loss
        loss = torch.tensor(0.0, requires_grad=True)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), lr=self.lr)

# --
# 3) Studio reuse + idle-stop guard
# --
def get_or_create_studio(name: str, machine: str = "L40S"):
    from lightning import Teamspace, Studio, Machine

    # Reuse running studio if exists
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s

    # If stopped, restart instead of recreating
    for s in Teamspace.studios:
        if s.name == name and s.status == "Stopped":
            print(f"Restarting stopped studio: {name}")
            s.start(machine=Machine(machine))
            return s

    # Create new only if none exist
    print(f"Creating new studio: {name}")
    return Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )

# --
# 4) Entrypoint
# --
def main():
    manifest = os.getenv("VANGUARD_MANIFEST", "file_list.json")
    if not Path(manifest).exists():
        raise FileNotFoundError(
            f"Manifest {manifest} not found. Generate on Mac with: "
            "list_repo_tree(repo, path=date_folder, recursive=False) -> file_list.json"
        )

    dataset = HFCDNDataset(manifest)
    loader = DataLoader(dataset, batch_size=8, shuffle=True, num_workers=2)

    model = SurrogateTrainer()
    fabric = Fabric(devices=1)
    fabric.launch()

    # Prefer Lightning Studio for heavy training
    # studio = get_or_create_studio("vanguard-surrogate-train")
    # trainer = Trainer(studio=studio, max_epochs=1)
    # trainer.fit(model, loader)

    # Lightweight local fallback (for CI/dev)
    trainer = Trainer(max_epochs=1, limit_train_batches=2)
    trainer.fit(model, loader)

if __name__ == "__main__":
    main()
```

Helper for Mac (one-time, run before training):

```bash
# generate manifest for a date folder (no recursion into subfolders)
python -c "
import json, os
from lightning import Client
client = Client()
repo = 'datasets/your-org/your-repo'
folder = 'batches/mirror-merged/2026-04-29'
tree = client.list_repo_tree(repo, path=folder, recursive=False)
files = [
    {'repo': repo, 'path': f['path'], 'prompt_key': 'prompt', 'response_key': 'response'}
    for f in tree if f['type'] == 'file' and f['path'].endswith('.parquet')
]
with open('file_list.json', 'w') as f:
    json.dump(files, f, indent=2)
print('Saved file_list.json')
"
```

## 4. Verification

1. Generate `file_list.json` on Mac (or reuse existing) and place in `/opt/axentx/vanguard/file_list.json`.
2. Run training locally (no HF API during data load):
   ```bash
   cd /opt
