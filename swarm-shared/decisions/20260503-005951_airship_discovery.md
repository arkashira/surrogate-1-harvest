# airship / discovery

## Incremental Improvement: Manifest-Driven CDN-Only Dataset Loader for Surrogate-1 Training

**Value**: Eliminates HF API rate limits (429), fixes `pyarrow.CastError` on mixed schemas, and enables 24/7 autonomous training by decoupling data loading from API quota. Ships in <2h.

---

### Implementation Plan

1. **Create manifest generator** (`scripts/generate-cdn-manifest.py`)  
   - Runs on Mac (or cron) after rate-limit window clears
   - Uses single `list_repo_tree` call per date folder
   - Outputs `manifest-{date}.json` with CDN URLs only

2. **Add CDN-only dataset loader** (`surrogate/data/cdn_dataset.py`)  
   - Reads manifest, streams files via `requests` (no auth)
   - Projects to `{prompt, response}` only at parse time
   - Drops `source`, `ts`, mixed schema cols before concat

3. **Update training script** (`surrogate/train.py`)  
   - Accepts `--manifest` arg
   - Uses `cdn_dataset.CDNSurrogateDataset`
   - Zero HF API calls during training

4. **Studio reuse guard**  
   - Check running studios before launch
   - Auto-restart if Lightning idle-stop kills training

---

### Code Snippets

#### 1. Manifest Generator
```python
# scripts/generate-cdn-manifest.py
#!/usr/bin/env python3
"""
Generate CDN-only manifest for Surrogate-1 training.
Run from Mac after HF rate-limit window clears.
"""
import json, os, sys
from datetime import datetime, timedelta
from huggingface_hub import HfApi

API_TOKEN = os.getenv("HF_TOKEN")
REPO_ID   = "axentx/surrogate-datasets"
DATE_DIR  = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")  # yesterday
OUT_FILE  = f"manifest-{DATE_DIR}.json"

def main():
    api = HfApi(token=API_TOKEN)
    # Single non-recursive call per folder
    entries = api.list_repo_tree(
        repo_id=REPO_ID,
        path=DATE_DIR,
        recursive=False
    )
    files = [e.rfilename for e in entries if e.rfilename.endswith(('.parquet', '.jsonl'))]
    manifest = {
        "date": DATE_DIR,
        "repo": REPO_ID,
        "files": [
            {
                "path": f,
                "cdn_url": f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{f}"
            }
            for f in sorted(files)
        ]
    }
    with open(OUT_FILE, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"✅ Manifest written to {OUT_FILE} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x scripts/generate-cdn-manifest.py
```

---

#### 2. CDN-Only Dataset Loader
```python
# surrogate/data/cdn_dataset.py
import json, requests, pyarrow as pa, pyarrow.parquet as pq, io
from torch.utils.data import IterableDataset

class CDNSurrogateDataset(IterableDataset):
    """Zero HF API calls. CDN-only, schema-projected dataset."""
    def __init__(self, manifest_path, columns=("prompt", "response")):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.columns = columns

    def _stream_file(self, url):
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return io.BytesIO(resp.content)

    def _project(self, table):
        # Keep only prompt/response; drop source/ts/mixed cols
        existing = set(table.column_names)
        keep = [c for c in self.columns if c in existing]
        if not keep:
            return None
        return table.select(keep)

    def __iter__(self):
        for entry in self.manifest["files"]:
            try:
                buf = self._stream_file(entry["cdn_url"])
                table = pq.read_table(buf)
                table = self._project(table)
                if table is None or table.num_rows == 0:
                    continue
                # Convert to dict batches
                for batch in table.to_batches():
                    cols = {k: batch.column(k).to_pylist() for k in batch.schema.names}
                    for i in range(batch.num_rows):
                        yield {k: cols[k][i] for k in cols}
            except Exception as e:
                print(f"⚠️  Skip {entry['path']}: {e}")
                continue
```

---

#### 3. Training Script Update
```python
# surrogate/train.py  (excerpt)
import argparse, os
from lightning.pytorch import Trainer
from lightning.pytorch.callbacks import ModelCheckpoint
from surrogate.data.cdn_dataset import CDNSurrogateDataset
from surrogate.model import SurrogateModel  # your existing model

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to CDN manifest JSON")
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    # Studio reuse guard
    from lightning.pytorch.cli import LightningCLI
    from lightning.pytorch import seed_everything
    seed_everything(42)

    dataset = CDNSurrogateDataset(args.manifest)
    # Use DataLoader with your preferred batch size
    from torch.utils.data import DataLoader
    loader = DataLoader(dataset, batch_size=8, num_workers=0)

    model = SurrogateModel()
    trainer = Trainer(
        max_epochs=args.epochs,
        accelerator="gpu",
        devices=1,
        callbacks=[ModelCheckpoint(monitor="loss")]
    )
    trainer.fit(model, loader)

if __name__ == "__main__":
    main()
```

---

#### 4. Studio Reuse + Idle Guard (Launcher)
```python
# surrogate/launch_studio.py
#!/usr/bin/env python3
from lightning.pytorch.cli import LightningCLI
from lightning.pytorch.studio import Studio, Teamspace
import sys

def reuse_or_create():
    team = Teamspace()
    for s in team.studios:
        if s.name == "surrogate-train" and s.status == "Running":
            print("🔁 Reusing running studio")
            return s
    print("🆕 Creating new studio")
    return Studio(
        name="surrogate-train",
        machine="L40S",
        cloud="lightning-public-prod",  # free tier fallback
        create_ok=True
    )

if __name__ == "__main__":
    studio = reuse_or_create()
    # Check idle stop before run
    if studio.status != "Running":
        studio.start(machine="L40S")
    studio.run(["train.py", "--manifest", "manifest-2026-05-02.json", "--epochs", "1"])
```

---

### Deployment Steps (2h)

```bash
cd /opt/axentx/airship

# 1. Install deps
pip install requests pyarrow huggingface_hub lightning

# 2. Generate manifest (after rate-limit window)
python scripts/generate-cdn-manifest.py

# 3. Test loader locally (quick smoke)
python -c "from surrogate.data.cdn_dataset import CDNSurrogateDataset; d=CDNSurrogateDataset('manifest-2026-05-02.json'); print(sum(1 for _ in d))"

# 4. Launch training via Studio (reuse guard)
python surrogate/launch_studio.py
```

**Cron note**: If scheduling `generate-cdn-manifest.py`, ensure crontab has:
```bash
SHELL=/bin/bash
0 6 * * * /usr/bin/python3 /opt/axentx/airship/scripts/generate-cdn-manifest.py
```
