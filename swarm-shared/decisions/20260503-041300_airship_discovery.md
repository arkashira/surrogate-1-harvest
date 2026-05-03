# airship / discovery

## Incremental Improvement: CDN-Only Training Pipeline + Lightning Resilience

**Goal**: Eliminate HF API 429s during Surrogate training and make Lightning training resilient to idle timeouts.  
**ETA**: <2h (no schema/infra changes; uses existing CDN paths and Lightning SDK).

---

## Implementation Plan

### 1. Pre-list file paths once (Mac orchestration)
Single API call to list one date folder, save to JSON. Embed in training script.

```bash
# scripts/list_hf_files.sh
#!/usr/bin/env bash
set -euo pipefail

REPO="datasets/surrogate-training"
DATE=$(date +%Y-%m-%d)
OUT="configs/file_list_${DATE}.json"

python3 -c "
import json, os
from huggingface_hub import list_repo_tree

files = list_repo_tree('$REPO', path='$DATE', recursive=False)
file_list = [f.rfilename for f in files if f.rfilename.endswith('.parquet')]

with open('$OUT', 'w') as f:
    json.dump({
        'date': '$DATE',
        'files': file_list,
        'base_url': f'https://huggingface.co/{REPO}/resolve/main/$DATE'
    }, f, indent=2)
print(f'Listed {len(file_list)} files -> $OUT')
"
```

### 2. CDN-only DataLoader (zero API calls during training)
Replace `load_dataset(streaming=True)` with direct CDN downloads via `hf_hub_download` fallback or raw HTTP.

```python
# surrogate/data/cdn_loader.py
import json, os, io, requests
import pyarrow.parquet as pq
from torch.utils.data import IterableDataset
from huggingface_hub import hf_hub_download

class CDNParquetDataset(IterableDataset):
    def __init__(self, file_list_path, cache_dir="./cache"):
        super().__init__()
        with open(file_list_path) as f:
            cfg = json.load(f)
        self.base_url = cfg["base_url"]
        self.files = cfg["files"]
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _stream_file(self, fname):
        # CDN direct (no auth, bypasses API rate limit)
        url = f"{self.base_url}/{fname}"
        r = requests.get(url, timeout=30, stream=True)
        r.raise_for_status()
        return io.BytesIO(r.content)

    def __iter__(self):
        for fname in self.files:
            try:
                buf = self._stream_file(fname)
                table = pq.read_table(buf)
                # Project to {prompt, response} only
                for row in table.select(["prompt", "response"]).to_pylist():
                    yield row
            except Exception as e:
                print(f"Skip {fname}: {e}")
                continue
```

### 3. Lightning Studio reuse + idle-resilient runner
List running studios, reuse if available; restart on idle stop.

```python
# surrogate/train/lightning_runner.py
import time
from lightning import Lightning, Teamspace, Machine

def get_or_create_studio(name="surrogate-train", machine="lightning-lambda-prod"):
    team = Teamspace()
    for s in team.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s
    print(f"Creating studio: {name}")
    return Lightning.studio(
        name=name,
        machine=Machine(machine),
        create_ok=True,
    )

def run_training_with_retry(script, max_retries=3):
    studio = get_or_create_studio()
    for attempt in range(max_retries):
        if studio.status != "Running":
            print(f"Studio stopped (idle), restarting...")
            studio = Lightning.studio(
                name=studio.name,
                machine=Machine("lightning-lambda-prod"),
                create_ok=True,
            )
        try:
            run = studio.run(script, name="surrogate-cdn-train")
            run.watch()
            if run.status == "succeeded":
                print("Training succeeded")
                return
            else:
                print(f"Run failed (attempt {attempt+1}): {run.status}")
        except Exception as e:
            print(f"Run error (attempt {attempt+1}): {e}")
        time.sleep(60)
    raise RuntimeError("Training failed after retries")

if __name__ == "__main__":
    run_training_with_retry("train.py")
```

### 4. Training script (CDN-only, no HF API during data load)

```python
# surrogate/train/train.py
import os
from torch.utils.data import DataLoader
from transformers import Trainer, TrainingArguments, AutoModelForCausalLM, AutoTokenizer
from surrogate.data.cdn_loader import CDNParquetDataset

def main():
    file_list = os.getenv("HF_FILE_LIST", "configs/file_list_latest.json")
    dataset = CDNParquetDataset(file_list)

    model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2.5-7B-Instruct")
    tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-7B-Instruct")

    training_args = TrainingArguments(
        output_dir="./outputs",
        per_device_train_batch_size=4,
        num_train_epochs=1,
        logging_steps=10,
        save_strategy="no",
        fp16=True,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
    )
    trainer.train()

if __name__ == "__main__":
    main()
```

### 5. Cron-safe wrapper (Bash shebang + executable)

```bash
#!/usr/bin/env bash
# scripts/run_surrogate_training.sh
set -euo pipefail
export SHELL=/bin/bash

cd /opt/axentx/airship/surrogate

# 1) List files once (respect HF rate limits)
./scripts/list_hf_files.sh

# 2) Run Lightning training (reuse studio, CDN-only)
python3 -m train.lightning_runner
```

```bash
chmod +x scripts/run_surrogate_training.sh
```

### 6. Crontab entry (safe)

```cron
SHELL=/bin/bash
0 2 * * * cd /opt/axentx/airship/surrogate && ./scripts/run_surrogate_training.sh >> logs/train_$(date +\%Y\%m\%d).log 2>&1
```

---

## Deployment Checklist (2h)

- [ ] `chmod +x scripts/*.sh`
- [ ] Test `list_hf_files.sh` manually (clears HF rate limit window first)
- [ ] Verify CDN URLs resolve (no 403/404)
- [ ] Run `python -m train.lightning_runner` once (creates studio)
- [ ] Confirm training completes without HF API calls (check logs for `resolve/main/` URLs)
- [ ] Enable cron with `SHELL=/bin/bash`

**Expected outcome**: Training runs with zero HF API auth calls during data loading (CDN-only), survives Lightning idle timeouts via studio reuse + restart logic, and avoids 429s entirely.
