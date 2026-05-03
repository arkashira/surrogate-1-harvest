# airship / discovery

## Final synthesized implementation (best of both proposals)

**Highest-value improvement**: Make Surrogate training **HF-rate-limit-proof** and **Lightning-idle-resilient** by generating a one-time CDN-only file list on the Mac and embedding it in training so Lightning workers do **zero HF API calls** during data loading, plus auto-restart stopped studios before `.run()`.

---

## 1) Mac-side: generate CDN file list (one-time / re-runnable)

`surrogate/training/gen_filelist.py`

```python
#!/usr/bin/env python3
"""
Generate CDN-only file list for a date folder.
Run on Mac (or any dev machine) — one-time or re-runnable.

Usage:
  HF_REPO=datasets/axentx/surrogate-dataset \
  DATE=2026-04-29 \
  OUT=cdn_filelist.json \
  python gen_filelist.py
"""
import os
import json
import argparse
from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.getenv("HF_REPO", "datasets/axentx/surrogate-dataset"))
    parser.add_argument("--date", default=os.getenv("DATE", "2026-04-29"))
    parser.add_argument("--out", default=os.getenv("OUT", "cdn_filelist.json"))
    args = parser.parse_args()

    prefix = f"batches/mirror-merged/{args.date}/"
    api = HfApi()
    tree = api.list_repo_tree(repo_id=args.repo, path=prefix, recursive=False)

    files = sorted(
        node.path
        for node in tree
        if node.path.endswith(".parquet")
    )

    meta = {"repo": args.repo, "date": args.date, "prefix": prefix, "files": files}
    with open(args.out, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

## 2) CDN-only dataloader (surrogate/training/train.py)

```python
#!/usr/bin/env python3
import argparse
import json
import pyarrow.parquet as pq
import requests
from io import BytesIO
from torch.utils.data import IterableDataset, DataLoader
import lightning as L

class CDNParquetStream(IterableDataset):
    """
    Stream parquet shards directly from CDN (no HF API calls during training).
    Projects to (prompt, response) only.
    """
    def __init__(self, file_list_path, columns=("prompt", "response")):
        super().__init__()
        with open(file_list_path) as f:
            meta = json.load(f)
        self.repo = meta["repo"]
        self.files = meta["files"]
        self.columns = columns

    def _stream_cdn(self, path):
        url = f"https://huggingface.co/datasets/{self.repo}/resolve/main/{path}"
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            buf = BytesIO(r.content)
            table = pq.read_table(buf, columns=self.columns)
            # Yield rows one-by-one to keep memory low
            for batch in table.to_batches(max_chunksize=1024):
                prompts = batch[0].to_pylist()
                responses = batch[1].to_pylist()
                yield from zip(prompts, responses)

    def __iter__(self):
        for path in self.files:
            yield from self._stream_cdn(path)

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, file_list, batch_size=8, num_workers=0):
        super().__init__()
        self.file_list = file_list
        self.batch_size = batch_size
        self.num_workers = num_workers

    def train_dataloader(self):
        ds = CDNParquetStream(self.file_list)
        return DataLoader(ds, batch_size=self.batch_size, num_workers=self.num_workers)

class SurrogateTrainer(L.LightningModule):
    def __init__(self, learning_rate=1e-4):
        super().__init__()
        self.save_hyperparameters()
        # TODO: define surrogate model here
        # self.model = ...

    def training_step(self, batch, batch_idx):
        prompts, responses = batch
        # surrogate training logic
        # loss = ...
        self.log("train_loss", 0.0, prog_bar=True)
        return 0.0

    def configure_optimizers(self):
        # placeholder
        return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list", default="cdn_filelist.json")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--limit-train-batches", type=int, default=None)
    args = parser.parse_args()

    dm = SurrogateDataModule(args.file_list, batch_size=args.batch_size)
    model = SurrogateTrainer()

    trainer = L.Trainer(
        max_epochs=args.epochs,
        limit_train_batches=args.limit_train_batches,
        enable_checkpointing=False,
        logger=False,
        enable_progress_bar=True,
    )
    trainer.fit(model, dm)

if __name__ == "__main__":
    main()
```

---

## 3) Lightning idle-resilient launcher (surrogate/training/launch.py)

```python
#!/usr/bin/env python3
"""
Lightning idle-resilient launcher.
- Reuses running Studio if available and status=running.
- Restarts if stopped before each run.
- Passes file list to training.
"""
import os
import time
import argparse
from lightning import LightningWork, LightningFlow, LightningApp, Machine, Studio

class SurrogateTrainerWork(LightningWork):
    def __init__(self, file_list="cdn_filelist.json"):
        super().__init__(
            machine=Machine.L40S,
            cloud_build_config=None,  # use default or specify if needed
        )
        self.file_list = file_list

    def run(self):
        import subprocess
        cmd = [
            "python", "train.py",
            "--file-list", self.file_list,
            "--batch-size", "8",
            "--epochs", "1",
        ]
        subprocess.run(cmd, check=True, cwd=os.path.dirname(__file__))

class SurrogateFlow(LightningFlow):
    def __init__(self, file_list="cdn_filelist.json"):
        super().__init__()
        self.file_list = file_list
        self.studio = None
        self.studio_name = "surrogate-train"

    def configure_layout(self):
        return []

    def run(self):
        # Try to reuse existing running studio
        if self.studio is None or self.studio.status != "running":
            running = [
                s for s in Studio.list()
                if s.name == self.studio_name and s.status == "running"
            ]
            if running:
                self.studio = running[0]
            else:
                self.studio = Studio(
                    name=self.studio_name,
                    target=SurrogateTrainerWork(file_list=self.file_list),
                    create_ok=True,
                )

        # Ensure studio is running before run
        if self.studio.status != "running":
            self.studio.start(machine=Machine.L40S)
            # brief wait for startup
            time.sleep(30)

        # Execute training (idempotent)
        self.studio.run()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list", default="cdn_filelist.json")
    args = parser.parse_args()
    app = LightningApp(SurrogateFlow(file_list=args.file_list))
```

---

## 4) Requirements (surrogate/training/requirements.txt)

```
lightning>=2.2
huggingface-hub
requests
pyarrow
torch
torchdata
```

---

## 5) One-command smoke test (Mac)

```bash
# 1) Generate CDN file list (one-time)
HF_REPO=datasets/axentx/surrogate-dataset \
DATE=2026-04-29 \
OUT=cdn_filelist.json \

