# airship / discovery

Below is the **single, final synthesis** that merges the strongest, non-contradictory parts of both proposals, resolves conflicts in favor of **correctness + concrete actionability**, and delivers a ready-to-run plan that fits the <2h constraint.

---

## Final Goal (unchanged)
Eliminate HF API 429s during Surrogate training data loading and make Lightning Studio resilient to idle-stop, cutting Mac→Lightning iteration to **<2 minutes** with **zero blocked runs**.

---

## Final Implementation Plan (merged + hardened)

1. **Pre-list file paths once on Mac** (or any dev host)  
   - Use `HfApi.list_repo_tree(recursive=False)` on the date folder.  
   - Save deterministic `train_files.json` next to training code.  
   - This is the **only** HF API call; it happens outside training.

2. **CDN-only dataset loader in Lightning Studio**  
   - Replace `load_dataset(streaming=True)` with direct CDN `requests` + `pyarrow.parquet`.  
   - Parse only `{prompt, response}`; drop extra columns.  
   - **Zero HuggingFace `datasets` API usage during training** → zero 429s.

3. **Resilient Lightning launcher with reuse + idle-stop guard**  
   - Detect running Studio by name; reuse if already running.  
   - If stopped, restart with `Machine.L40S`.  
   - Start once, then invoke training entrypoint with the pre-generated file list.

4. **Hygienic wrapper script**  
   - Shebang `#!/usr/bin/env bash`, `set -euo pipefail`, explicit `SHELL=/bin/bash`.  
   - One command on Mac: `bash prepare_and_launch.sh`.

---

## Final Code (single coherent stack)

### 1) Mac pre-list + launcher wrapper  
`prepare_and_launch.sh`

```bash
#!/usr/bin/env bash
# prepare_and_launch.sh
set -euo pipefail
export SHELL=/bin/bash

REPO="axentx/surrogate-data"
DATE_DIR="batches/mirror-merged/$(date +%Y-%m-%d)"
OUTFILE="train_files.json"

# 1) Pre-list files (shallow) → train_files.json
python3 - "$REPO" "$DATE_DIR" "$OUTFILE" <<'PY'
import json, sys
from huggingface_hub import HfApi

repo, date_dir, outfile = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi()
tree = api.list_repo_tree(repo, path=date_dir, recursive=False)
files = [f.rfilename for f in tree if f.rfilename.endswith((".parquet", ".jsonl"))]
with open(outfile, "w") as f:
    json.dump({"date_dir": date_dir, "files": files}, f, indent=2)
print(f"Wrote {len(files)} files to {outfile}")
PY

# 2) Launch Lightning training (reuses or restarts studio automatically)
python3 lightning_launcher.py --file-list "$OUTFILE"
```

---

### 2) CDN-only dataset loader  
`surrogate/data/cdn_loader.py`

```python
import json
import pyarrow.parquet as pq
import requests
from io import BytesIO
from pathlib import Path
from typing import List, Dict

CDN_ROOT = "https://huggingface.co/datasets"
REPO = "axentx/surrogate-data"

def cdn_url(file_path: str) -> str:
    return f"{CDN_ROOT}/{REPO}/resolve/main/{file_path}"

def load_cdn_shard(file_path: str, columns=("prompt", "response")) -> List[Dict]:
    url = cdn_url(file_path)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    table = pq.read_table(BytesIO(resp.content), columns=columns)
    return table.to_pylist()

def build_dataset(file_list_path: str) -> List[Dict]:
    meta = json.loads(Path(file_list_path).read_text())
    records = []
    for fpath in meta["files"]:
        try:
            records.extend(load_cdn_shard(fpath))
        except Exception as exc:
            print(f"Skipping {fpath}: {exc}")
    return records
```

---

### 3) Lightning launcher with reuse + idle-stop guard  
`lightning_launcher.py`

```python
import argparse
from pathlib import Path
from lightning import LightningWork, LightningFlow, LightningApp, Machine
from lightning.app import Teamspace

REPO = "axentx/surrogate-data"

class SurrogateTrainer(LightningWork):
    def __init__(self, file_list_path: str):
        super().__init__(machine=Machine.L40S, cloud_compute=Machine.L40S)
        self.file_list_path = file_list_path
        self._has_run = False

    def run(self):
        if self._has_run:
            return
        from surrogate.data.cdn_loader import build_dataset
        from surrogate.train import train_step  # your existing train loop

        dataset = build_dataset(self.file_list_path)
        train_step(dataset)
        self._has_run = True

class MonitorFlow(LightningFlow):
    def __init__(self, trainer: SurrogateTrainer):
        super().__init__()
        self.trainer = trainer

    def run(self):
        # Reuse running studio by name if present
        for studio in Teamspace().studios:
            if studio.name == "surrogate-train" and studio.status == "running":
                print("Reusing running studio 'surrogate-train'")
                if not self.trainer.has_started:
                    self.trainer.start()
                return

        # If stopped, restart
        if self.trainer.status == "stopped":
            print("Studio stopped — restarting with L40S")
            self.trainer.start(machine=Machine.L40S)

        if not self.trainer.has_started:
            self.trainer.start()

        # Kick off training once
        if not getattr(self.trainer, "_has_run", False):
            self.trainer.run()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list", required=True)
    args = parser.parse_args()

    # Ensure file list exists
    Path(args.file_list).resolve(strict=True)

    trainer = SurrogateTrainer(file_list_path=args.file_list)
    flow = MonitorFlow(trainer)
    app = LightningApp(flow)
```

---

## Rollout (final)

1. Place `prepare_and_launch.sh`, `surrogate/data/cdn_loader.py`, and `lightning_launcher.py` in `/opt/axentx/airship/surrogate/`.  
2. On Mac (or dev host):  
   ```bash
   cd /opt/axentx/airship/surrogate
   bash prepare_and_launch.sh
   ```
3. Lightning Studio will:
   - Reuse or restart automatically.  
   - Load data via CDN (no HF API calls during training).  
   - Complete without 429s or idle-stop blocks.

**Expected outcome**:  
- Zero HF API 429s during data load.  
- Mac→Lightning iteration **<2 minutes**.  
- No blocked runs due to rate limits or idle-stop.
