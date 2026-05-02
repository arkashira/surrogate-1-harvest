# vanguard / backend

## Final Synthesized Solution

**Core diagnosis (unified):**  
- HF API 429 from repeated repo enumeration and schema probing during training.  
- No persisted file manifest → every run re-lists repos and re-checks schemas.  
- Lightning Studio is recreated instead of reused; idle-stop kills sessions and burns quota.  
- Streaming over heterogeneous repo files triggers runtime pyarrow `CastError`.  
- No CDN bypass: training still performs authenticated API requests instead of raw CDN fetches.  
- No guard to restart stopped studios before `.run()` → silent failures.

**Chosen strategy (correct + actionable):**  
1. Generate a one-time **file manifest** per date folder (Mac/dev) and commit it.  
2. Train using **CDN URLs only** (no HF API auth during training) with strict schema projection to avoid `CastError`.  
3. **Reuse or restart** existing Lightning Studio to avoid recreation and idle-stop deaths.  
4. Add an **idle-restart guard** before `.run()` and a small streaming training loop.

---

## Implementation

File: `/opt/axentx/vanguard/train.py`

```python
# /opt/axentx/vanguard/train.py
import json
import os
import time
from pathlib import Path
from typing import List, Dict

import requests
import torch
from datasets import load_dataset, Features, Value
from lightning import Fabric

# ---------- Configuration ----------
HF_REPO = os.getenv("HF_REPO", "datasets/your-org/surrogate-1")
MANIFEST_PATH = Path(os.getenv("MANIFEST_PATH", "file_manifest.json"))
HF_TOKEN = os.getenv("HF_TOKEN", "")  # optional; used only for manifest generation
CDN_BASE = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

# ---------- Manifest utilities ----------
def build_manifest(date_folder: str, out_path: Path = MANIFEST_PATH) -> List[str]:
    """
    One-time call (on Mac or dev machine) to list files under a single date folder.
    Requires huggingface_hub only for manifest generation.
    """
    try:
        from huggingface_hub import list_repo_tree
    except ImportError as e:
        raise RuntimeError("huggenface_hub required for build_manifest only") from e

    entries = list_repo_tree(
        repo_id=HF_REPO,
        path=date_folder,
        repo_type="dataset",
        token=HF_TOKEN or None,
    )
    files = [e.rfilename for e in entries if e.type == "file"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(files, indent=2))
    return files

def load_manifest(manifest_path: Path = MANIFEST_PATH) -> List[str]:
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Manifest missing: {manifest_path}. Generate it once with build_manifest()."
        )
    return json.loads(manifest_path.read_text())

# ---------- CDN-backed streaming ----------
def cdn_urls(file_paths: List[str]) -> List[str]:
    return [f"{CDN_BASE}/{p}" for p in file_paths]

def parse_parquet_for_surrogate(batch: Dict) -> Dict:
    """
    Keep only {prompt, response}. Drop extra fields to avoid schema drift/cast errors.
    Assumes files are parquet with at least 'prompt' and 'response' fields.
    """
    return {
        "prompt": batch["prompt"],
        "response": batch["response"],
    }

def make_cdn_dataset(file_urls: List[str], split: str = "train"):
    """
    Uses CDN URLs directly (no HF API auth during training).
    Streaming + strict projection to avoid pyarrow CastError.
    """
    # Minimal feature spec to enforce schema and avoid surprises
    features = Features({
        "prompt": Value("string"),
        "response": Value("string"),
    })

    ds = load_dataset(
        "parquet",
        data_files={"train": file_urls},
        streaming=True,
        split=split,
        features=features,
    )
    # Remove any extra columns and cast safely
    return ds.map(parse_parquet_for_surrogate, remove_columns=ds.features.keys())

# ---------- Lightning Studio reuse + idle guard ----------
def get_or_create_studio(name: str, machine: str = "L40S"):
    """
    Reuse running studio if exists; restart if stopped.
    Avoids quota burn from recreation and silent idle-stop deaths.
    """
    try:
        from lightning.app import Studio
        studios = Studio.list()
        for s in studios:
            if s.name == name:
                if s.status == "running":
                    print(f"Reusing running studio: {name}")
                    return s
                else:
                    print(f"Studio {name} exists but status={s.status}. Restarting...")
                    s.start(machine=machine)
                    return s
    except Exception as e:
        print(f"Studio reuse check failed ({e}). Will create new.")

    print(f"Creating new studio: {name}")
    return Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )

# ---------- Training entry ----------
def train():
    studio = get_or_create_studio("vanguard-surrogate-train", machine="L40S")

    # Ensure manifest exists (should be committed/generated on Mac)
    if not MANIFEST_PATH.is_file():
        print("No manifest found. If on Mac, run:")
        print(f"  python -c \"from train import build_manifest; build_manifest('batches/mirror-merged/2026-05-02')\"")
        return

    files = load_manifest()
    urls = cdn_urls(files)
    dataset = make_cdn_dataset(urls)

    fabric = Fabric(accelerator="cuda", devices=1, precision="bf16-mixed")
    fabric.launch()

    # Minimal surrogate training loop (replace with your model/tokenizer)
    model = torch.nn.Linear(1024, 1024)  # placeholder
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)

    model, optimizer = fabric.setup(model, optimizer)

    step = 0
    for item in dataset:
        # Dummy tensor example — replace with tokenized inputs
        x = torch.randn(4, 1024)
        y = torch.randn(4, 1024)
        x, y = fabric.to_device(x), fabric.to_device(y)

        optimizer.zero_grad()
        loss = torch.nn.functional.mse_loss(model(x), y)
        fabric.backward(loss)
        optimizer.step()

        if step % 10 == 0:
            print(f"step {step} loss {loss.item():.4f}")
        step += 1
        if step >= 100:
            break

    print("Training complete (demo loop).")

if __name__ == "__main__":
    train()
```

Helper script for Mac (run once per date folder, then commit manifest):

```bash
#!/usr/bin/env bash
# /opt/axentx/vanguard/build_manifest.sh
set -euo pipefail
cd /opt/axentx/vanguard
python -c "from train import build_manifest; build_manifest('batches/mirror-merged/2026-05-02')"
```

---

## Verification

1. Generate manifest (Mac/dev):
   ```bash
   cd /opt/axentx/vanguard
   python -c "from train import build_manifest; build_manifest('batches/mirror-merged/2026-05-02')"
   ```
   Confirm `file_manifest.json` exists and contains relative paths.

2. Confirm CDN URLs are reachable without token:
   ```bash
   head -1 file_manifest.json | python -c "import sys,json; f=json.load(sys.stdin)[0]; import requests; r=requests.get('https://huggingface.co/datasets/your-org/surrogate-1/resolve/main/' + f, timeout=5); print(r.status_code, len(r.content))"
   ```
   Expect `200` and non-zero length.

3. Run training (dry-run limited steps):
   ```bash
   cd /opt/axentx/vanguard
   python train.py
   ```
   Expect:
   - Reuse/create studio message.
   - Manifest load success.
   - Streaming dataset
