# vanguard / backend

## Final Synthesized Implementation  
*(Best parts merged, contradictions resolved in favor of correctness + concrete actionability)*

### 1. Diagnosis (resolved)
- **Rate-limit root cause**: repeated `list_repo_tree`/`load_dataset` discovery and per-worker HF API auth hits.  
  **Fix**: single non-recursive snapshot → JSON manifest → CDN-only data loading (no Authorization header).
- **Quota burn**: Lightning Studio create/idle/stop cycles + idle-stop kills.  
  **Fix**: explicit reuse-or-restart logic; prefer L40S free tier; avoid idle-stop by keeping alive only for training window.
- **Schema drift**: mixed-schema ingestion (extra `source`, `ts`) breaks surrogate-1.  
  **Fix**: hard project to `{prompt,response}`; validate before training; fail fast if missing.
- **Local resource burn**: heavy `from_pretrained` on Mac.  
  **Fix**: delegate model load to Lightning/Cerebras; Mac only orchestrates manifest + triggers remote run.
- **CDN bypass missing**: every DataLoader worker hits HF auth endpoints.  
  **Fix**: download once per node to `/tmp/vanguard_cache` via CDN URLs; memory-map with `datasets`/`parquet`.

---

### 2. Files (minimal, production-ready)

#### `/opt/axentx/vanguard/backend/manifest.py`
```python
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/surrogate-1")
MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

def snapshot_date_folder(date_folder: str, out_path: Path | None = None) -> Path:
    """
    Single non-recursive API call.
    Returns manifest with CDN URLs (no auth required).
    """
    api = HfApi()
    tree = api.list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=False)
    files = [
        {
            "path": f.rfilename,
            "cdn_url": (
                f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/"
                f"{date_folder}/{f.rfilename}"
            ),
        }
        for f in tree if not f.type == "directory"
    ]
    if not files:
        raise ValueError(f"No files found in {HF_REPO}/{date_folder}")

    manifest = {
        "repo": HF_REPO,
        "date_folder": date_folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    out_path = out_path or (MANIFEST_DIR / f"{date_folder}.json")
    out_path.write_text(json.dumps(manifest, indent=2))
    return out_path

if __name__ == "__main__":
    import sys
    date_folder = sys.argv[1] if len(sys.argv) > 1 else "batches/mirror-merged/2026-05-02"
    p = snapshot_date_folder(date_folder)
    print(f"Manifest written: {p}")
```

---

#### `/opt/axentx/vanguard/backend/train.py`
```python
import json
import os
from pathlib import Path
from typing import Dict

import torch
from datasets import load_dataset
from lightning import Fabric
from torch.utils.data import DataLoader
from transformers import AutoModelForCausalLM, AutoTokenizer

MANIFEST_DIR = Path(__file__).parent / "manifests"
CACHE_ROOT = Path(os.getenv("VANGUARD_CACHE", "/tmp/vanguard_cache"))
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

def load_manifest(date_folder: str) -> Dict:
    p = MANIFEST_DIR / f"{date_folder}.json"
    if not p.exists():
        raise FileNotFoundError(f"Manifest missing: {p}. Run manifest.py first.")
    return json.loads(p.read_text())

def _download_cdn_files(manifest: Dict) -> list:
    paths = []
    import urllib.request
    for m in manifest["files"]:
        out = CACHE_ROOT / m["path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            urllib.request.urlretrieve(m["cdn_url"], out)
        paths.append(str(out))
    return paths

def cdn_only_dataset(manifest: Dict, split="train"):
    """
    Downloads once via CDN (no auth), then loads with datasets.
    Projects to {prompt,response} and validates schema.
    """
    paths = _download_cdn_files(manifest)
    # Prefer parquet; fallback to jsonl/csv if needed
    ext_to_type = {".parquet": "parquet", ".jsonl": "json", ".json": "json", ".csv": "csv"}
    grouped = {}
    for p in paths:
        key = ext_to_type.get(Path(p).suffix.lower())
        if key is None:
            continue
        grouped.setdefault(key, []).append(p)

    if not grouped:
        raise ValueError("No supported data files (parquet/jsonl/json/csv) in manifest.")

    # Use first available format; for mixed formats, unify to parquet externally.
    fmt, fpaths = next(iter(grouped.items()))
    ds = load_dataset(fmt, data_files={split: fpaths}, split=split)

    # Schema hardening for surrogate-1
    required = {"prompt", "response"}
    missing = required - set(ds.column_names)
    if missing:
        raise ValueError(f"Missing required columns: {missing}. Found: {ds.column_names}")
    ds = ds.select_columns(list(required))
    return ds

def run_training(date_folder: str, max_steps: int = 100):
    fabric = Fabric(accelerator="cuda", devices=1, precision="bf16-mixed")
    fabric.launch()

    manifest = load_manifest(date_folder)
    ds = cdn_only_dataset(manifest)
    dl = DataLoader(ds, batch_size=8, shuffle=True, num_workers=0)
    dl = fabric.setup_dataloaders(dl)

    model = AutoModelForCausalLM.from_pretrained(
        "openai-community/gpt2", torch_dtype=torch.bfloat16
    )
    tokenizer = AutoTokenizer.from_pretrained("openai-community/gpt2")
    tokenizer.pad_token = tokenizer.eof_token
    model.resize_token_embeddings(len(tokenizer))
    model = fabric.setup(model)

    model.train()
    step = 0
    for batch in dl:
        if step >= max_steps:
            break
        batch = tokenizer(
            batch["prompt"],
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=512,
        )
        batch = {k: v.to(fabric.device) for k, v in batch.items()}
        out = model(**batch)
        loss = out.loss
        fabric.backward(loss)
        fabric.print(f"step={step} loss={loss.item():.4f}")
        step += 1

    fabric.save("checkpoint.pt", {"model": model.state_dict()})
    print("Training complete. Checkpoint saved.")

if __name__ == "__main__":
    import sys
    date_folder = sys.argv[1] if len(sys.argv) > 1 else "batches/mirror-merged/2026-05-02"
    run_training(date_folder, max_steps=int(os.getenv("MAX_STEPS", "100")))
```

---

#### `/opt/axentx/vanguard/backend/orchestrator.py`
```python
import os
import sys
from pathlib import Path

from .manifest import snapshot_date_folder
from .train import run_training

try:
    from lightning import Studio, Machine
    _LIGHTNING_AVAILABLE = True
except Exception:
    _LIGHTNING_AVAILABLE = False

def reuse_or_create_studio(name: str = "vanguard-surrogate-train"):
    """
    Reuse a running Lightning Studio; if stopped, restart on L40S.
    Avoids quota burn from repeated create/idle/stop cycles.
    """
    if not _LIGHTNING_AVAILABLE:
        print("Lightning not available; skipping studio reuse (local/dev mode).")
        return None

    from lightning import Teamspace
    studios = Teamspace.studios()
    running = [s for s in studios if s.name == name and s.status == "running"]
    if running:

