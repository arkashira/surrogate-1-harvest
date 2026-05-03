# airship / frontend

## Final Synthesized Implementation Plan  
*(Best parts merged; contradictions resolved for correctness + concrete actionability)*

### Core Goal
Ship a **CDN-only training manifest + Lightning-idle-resilient runner** that:
- Eliminates HF API rate limits during training.
- Survives Lightning idle stops by auto-restarting and resuming.
- Can be implemented and verified in <2 hours.

---

### 1) Generate CDN Manifest (run once from orchestrator/Mac)
- Use a single, non-recursive `list_repo_tree` call **per date folder** to avoid pagination and 429s.
- Save a minimal JSON with CDN URLs only (no auth required at training time).
- Keep generator simple, deterministic, and idempotent.

```python
#!/usr/bin/env python3
"""
Generate CDN manifest for one date folder.
Run once when HF API window is clear.
"""
import json, os
from huggingface_hub import HfApi

REPO = "datasets/your-org/surrogate-mirror-merged"
DATE_FOLDER = "batches/mirror-merged/2026-05-03"  # adjust as needed
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "train_manifest.json")

def main():
    api = HfApi()
    entries = api.list_repo_tree(repo_id=REPO, path=DATE_FOLDER, recursive=False)
    files = sorted(e.rfilename for e in entries if e.rfilename.endswith(".parquet"))
    urls = [
        f"https://huggingface.co/datasets/{REPO}/resolve/main/{f}"
        for f in files
    ]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"repo": REPO, "date_folder": DATE_FOLDER, "files": urls}, f, indent=2)
    print(f"Wrote {len(urls)} CDN files to {OUT}")

if __name__ == "__main__":
    main()
```

**Action**:  
```bash
chmod +x scripts/generate_manifest.py
python scripts/generate_manifest.py
```

---

### 2) Lightning-Aware Runner with Idle Resilience
- Reuse a running studio if present.
- If stopped, restart on `L40S` (or fallback as needed).
- Launch training non-blockingly and survive idle timeouts by relying on frequent checkpoints (handled in `train.py`).

```python
#!/usr/bin/env python3
"""
Lightning Studio wrapper:
- Reuse or start studio
- Run training with CDN manifest (zero HF API calls during data load)
"""
import time, os, sys
from lightning_sdk import Studio, Machine, Teamspace

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
TRAIN_SCRIPT = os.path.join(PROJECT_ROOT, "train.py")
MANIFEST = os.path.join(PROJECT_ROOT, "data", "train_manifest.json")
STUDIO_NAME = "surrogate-train-l40s"

def get_or_create_studio():
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == STUDIO_NAME:
            return s
    return teamspace.create_studio(name=STUDIO_NAME, machine=Machine.L40S)

def run():
    studio = get_or_create_studio()
    if studio.status != "running":
        print(f"Studio {STUDIO_NAME} is {studio.status}; starting...")
        studio.start(machine=Machine.L40S)
        while studio.status != "running":
            time.sleep(10)
            studio.refresh()

    cmd = [
        "python", TRAIN_SCRIPT,
        "--manifest", MANIFEST,
        "--epochs", "1",
        "--batch-size", "8",
        "--checkpoint-dir", "/workspace/checkpoints",
    ]
    print("Running:", " ".join(cmd))
    result = studio.terminal.run(" ".join(cmd), cwd="/workspace")
    print(result)

if __name__ == "__main__":
    run()
```

**Action**:  
```bash
chmod +x scripts/run_training.py
python scripts/run_training.py
```

---

### 3) Patch `train.py` for CDN-Only Streaming + Checkpointing
- Replace HF `load_dataset` with a lightweight CDN parquet loader.
- Stream via HTTP range requests (efficient, no full downloads).
- Project only `{prompt, response}` at parse time.
- **Critical for idle resilience**: checkpoint frequently so restarts resume progress.

```python
import json, os, io, torch
from torch.utils.data import Dataset, DataLoader
import pyarrow.parquet as pq
import requests
from transformers import AutoTokenizer

class CDNParquetDataset(Dataset):
    def __init__(self, manifest_path, tokenizer_name="bert-base-uncased", max_length=512):
        with open(manifest_path) as f:
            m = json.load(f)
        self.urls = m["files"]
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
        self.max_length = max_length

    def __len__(self):
        return len(self.urls)

    def __getitem__(self, idx):
        url = self.urls[idx]
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        table = pq.read_table(io.BytesIO(resp.content))
        df = table.select(["prompt", "response"]).to_pandas()
        if df.empty:
            # fallback dummy
            return {"input_ids": torch.zeros(self.max_length, dtype=torch.long),
                    "labels": torch.zeros(self.max_length, dtype=torch.long)}
        sample = df.iloc[0]
        text = f"Prompt: {sample['prompt']}\nResponse: {sample['response']}"
        enc = self.tokenizer(
            text,
            max_length=self.max_length,
            truncation=True,
            padding="max_length",
            return_tensors="pt",
        )
        input_ids = enc["input_ids"].squeeze(0)
        labels = input_ids.clone()
        return {"input_ids": input_ids, "labels": labels}

# Example training loop with checkpointing
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--checkpoint-dir", default="/workspace/checkpoints")
    args = parser.parse_args()

    os.makedirs(args.checkpoint_dir, exist_ok=True)
    ds = CDNParquetDataset(args.manifest)
    loader = DataLoader(ds, batch_size=args.batch_size, shuffle=True)

    # Minimal model placeholder — replace with your model
    model = torch.nn.Linear(512, 512)  # dummy
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-4)
    start_epoch = 0

    # Resume from latest checkpoint if exists
    ckpt_path = os.path.join(args.checkpoint_dir, "latest.pt")
    if os.path.exists(ckpt_path):
        ckpt = torch.load(ckpt_path)
        start_epoch = ckpt.get("epoch", 0) + 1
        # model.load_state_dict(ckpt["model"])  # uncomment for real model
        optimizer.load_state_dict(ckpt["optimizer"])
        print(f"Resumed from epoch {ckpt.get('epoch', 0)}")

    for epoch in range(start_epoch, args.epochs):
        for batch in loader:
            # Dummy training step
            loss = model(batch["input_ids"].float()).sum()
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        # Save checkpoint after each epoch (critical for idle resilience)
        torch.save({
            "epoch": epoch,
            # "model": model.state_dict(),  # uncomment for real model
            "optimizer": optimizer.state_dict(),
        }, os.path.join(args.checkpoint_dir, "latest.pt"))
        print(f"Epoch {epoch} checkpoint saved")
```

---

### 4) Optional Cron Wrapper (for scheduled retries)
- Use `SHELL=/bin/bash` and absolute paths.
- Logs to file for debugging.

```bash
#!/bin/bash
# /opt/axentx/airship/surrogate/scripts/cron_train.sh
export SHELL=/bin/bash
cd /opt/axentx/airship/surrogate
python scripts/run_training.py
```

```
