# airship / discovery

**Synthesized Final Implementation Plan (≤ 2 h)**

**Highest-value improvement**  
Add a **zero-API-training mode** plus **Lightning auto-recovery** so Surrogate training is HF-rate-limit-proof and survives idle timeouts.

---

## 1) One-time Mac manifest builder (15 min)

`scripts/build_cdn_manifest.py`

```python
#!/usr/bin/env python3
"""
One-time builder (run after HF rate-limit window clears).
Produces training/cdn_manifest.json for zero-API training.
"""
import json
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi

REPO = "axentx/surrogate-mirror"
DATE_FOLDER = datetime.utcnow().strftime("%Y-%m-%d")  # or override via arg
OUT_PATH = Path(__file__).parent.parent / "training" / "cdn_manifest.json"

def main() -> None:
    api = HfApi()
    entries = api.list_repo_tree(repo_id=REPO, path=DATE_FOLDER, recursive=False)

    files = []
    for e in entries:
        if not e.path.endswith(".parquet"):
            continue
        cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{e.path}"
        files.append({"path": e.path, "cdn_url": cdn_url})

    manifest = {
        "repo": REPO,
        "date": DATE_FOLDER,
        "created_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
        "total": len(files),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files -> {OUT_PATH}")

if __name__ == "__main__":
    main()
```

```bash
chmod +x scripts/build_cdn_manifest.py
# Run once (or via cron after rate-limit window):
python scripts/build_cdn_manifest.py
```

---

## 2) Zero-API streaming dataset (25 min)

`training/cdn_streaming_dataset.py`

```python
import io
import json
from typing import Dict

import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import IterableDataset

class CDNStreamingDataset(IterableDataset):
    def __init__(self, manifest_path: str):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = [f["cdn_url"] for f in self.manifest["files"]]

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            it = iter(self.files)
        else:
            per_worker = max(1, len(self.files) // worker_info.num_workers)
            rank = worker_info.id
            it = iter(self.files[rank * per_worker : (rank + 1) * per_worker])

        for url in it:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            table = pq.read_table(io.BytesIO(resp.content))
            # Project to {prompt, response} only
            df = table.select(["prompt", "response"]).to_pandas()
            for _, row in df.iterrows():
                yield {
                    "prompt": str(row["prompt"]),
                    "response": str(row["response"]),
                }
```

---

## 3) Lightning auto-recovery wrapper + training script (30 min)

`training/lightning_trainer.py`

```python
#!/usr/bin/env python3
"""
Lightning wrapper that:
- checks Studio status,
- auto-restarts stopped Studio,
- pins Machine.L40S,
- launches train.py with manifest.
"""
import argparse
import subprocess
import sys
from pathlib import Path

import lightning as L

def ensure_studio_running(machine: L.Machine = L.Machine.L40S) -> None:
    studio = L.Studio()
    if studio.status == "Stopped":
        target = L.Target()
        target.start(machine=machine)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output_dir", type=Path, default=Path("outputs"))
    parser.add_argument("--max_steps", type=int, default=1000)
    args = parser.parse_args()

    ensure_studio_running()

    train_script = Path(__file__).parent / "train.py"
    cmd = [
        sys.executable,
        str(train_script),
        "--manifest",
        str(args.manifest),
        "--output_dir",
        str(args.output_dir),
        "--max_steps",
        str(args.max_steps),
    ]
    subprocess.run(cmd, check=True)

if __name__ == "__main__":
    main()
```

`training/train.py` (minimal patch)

```python
import argparse
from pathlib import Path

import lightning as L
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from cdn_streaming_dataset import CDNStreamingDataset

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output_dir", type=Path, default=Path("outputs"))
    parser.add_argument("--max_steps", type=int, default=1000)
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")
    model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")

    ds = CDNStreamingDataset(args.manifest)
    loader = torch.utils.data.DataLoader(ds, batch_size=8, num_workers=2)

    trainer = L.Trainer(
        max_steps=args.max_steps,
        accelerator="gpu",
        devices=1,
        default_root_dir=args.output_dir,
    )
    trainer.fit(model, loader)

if __name__ == "__main__":
    main()
```

---

## 4) Run instructions & cron guidance (10 min)

Update `surrogate/README.md`

```markdown
## Zero-API training (rate-limit-proof)

1. Build manifest (Mac):
   ```bash
   python scripts/build_cdn_manifest.py
   ```

2. Launch training with auto-recovery:
   ```bash
   python training/lightning_trainer.py \
     --manifest training/cdn_manifest.json \
     --output_dir outputs \
     --max_steps 1000
   ```

3. Cron example (daily after rate-limit window):
   ```cron
   SHELL=/bin/bash
   0 6 * * * cd /path/to/surrogate && python scripts/build_cdn_manifest.py && python training/lightning_trainer.py --manifest training/cdn_manifest.json --max_steps 1000 >> logs/train.log 2>&1
   ```
```

---

**Total estimated time:** ~90 min  
**Key wins:**  
- Zero-API training via CDN URLs (rate-limit-proof).  
- Lightning auto-recovery for idle timeouts.  
- Idempotent schema projection (`prompt`, `response` only).
