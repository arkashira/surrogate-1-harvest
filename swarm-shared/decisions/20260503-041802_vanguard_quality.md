# vanguard / quality

## 1. Diagnosis
- No content-addressed manifest exists → training/frontend hit HF API at runtime, causing 429s and non-reproducible epochs.
- Mixed-schema `enriched/` files include `source`/`ts` columns that break `load_dataset` expectations for surrogate-1.
- Lightning Studio churn (create/stop/recreate) wastes quota and risks idle-stop training death.
- No CDN-only data path — ingestion/training rely on authenticated `/api/` calls instead of public CDN URLs.
- No deterministic repo selection for HF commit-cap mitigation (single repo risks 128/hr limit).

## 2. Proposed change
Create `/opt/axentx/vanguard/training/manifest.py` and update `/opt/axentx/vanguard/training/train.py` to:
- Generate a content-addressed manifest (JSON) after ingestion: `{date, slug, hf_repo, path, sha256, rows}`.
- Embed the manifest path in `train.py`; data loader uses CDN-only URLs (`resolve/main/...`) with zero API calls.
- Project to `{prompt, response}` on-the-fly and skip rows with malformed schema.
- Reuse a running Lightning Studio by name; restart only if stopped.

## 3. Implementation
```bash
# /opt/axentx/vanguard/training/manifest.py
import json, hashlib, os
from pathlib import Path
from typing import List, Dict

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

def hash_slug(parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]

def build_manifest(date: str, hf_repo: str, folder_path: str, file_infos: List[Dict]) -> Dict:
    entries = []
    for fi in file_infos:
        path = fi["path"]
        rows = fi.get("rows", 0)
        sha256 = fi.get("sha256", "")
        slug = hash_slug([date, path])
        entries.append({
            "date": date,
            "slug": slug,
            "hf_repo": hf_repo,
            "path": path,
            "sha256": sha256,
            "rows": rows,
            "cdn_url": f"https://huggingface.co/datasets/{hf_repo}/resolve/main/{path}"
        })
    manifest = {"date": date, "hf_repo": hf_repo, "folder": folder_path, "entries": entries}
    out = MANIFEST_DIR / f"manifest-{date}.json"
    out.write_text(json.dumps(manifest, indent=2))
    return manifest

def load_manifest(date: str):
    p = MANIFEST_DIR / f"manifest-{date}.json"
    if not p.exists():
        raise FileNotFoundError(f"Manifest not found: {p}")
    return json.loads(p.read_text())
```

```python
# /opt/axentx/vanguard/training/train.py  (excerpt to insert/replace)
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from lightning import Fabric, LightningModule, Trainer
from lightning.pytorch.loggers import CSVLogger
from .manifest import load_manifest, MANIFEST_DIR
import os

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-data")
DATE = os.getenv("TRAIN_DATE", "2026-04-29")

def cdn_rows_generator(manifest):
    for ent in manifest["entries"]:
        url = ent["cdn_url"]
        # stream CDN file (no auth, no API)
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            # project to {prompt,response} on-the-fly
            try:
                table = pq.read_table(pa.BufferReader(r.content), columns=["prompt", "response"])
            except (pa.ArrowInvalid, KeyError):
                # skip malformed/mixed-schema files
                continue
            for batch in table.to_batches(max_chunksize=512):
                df = batch.to_pandas()
                for _, row in df.iterrows():
                    if not isinstance(row.get("prompt"), str) or not isinstance(row.get("response"), str):
                        continue
                    yield {"prompt": row["prompt"], "response": row["response"]}

def make_dataloader(manifest, batch_size=8):
    import torch
    from torch.utils.data import IterableDataset, DataLoader
    class CDNIterable(IterableDataset):
        def __init__(self, manifest):
            self.manifest = manifest
        def __iter__(self):
            return cdn_rows_generator(self.manifest)
    ds = CDNIterable(manifest)
    return DataLoader(ds, batch_size=batch_size, num_workers=0)

# Lightning reuse + idle-stop resilience
from lightning import Teamspace, Studio, Machine

def get_or_create_studio(name="vanguard-surrogate-train"):
    ts = Teamspace()
    for s in ts.studios:
        if s.name == name and s.status == "Running":
            return s
    # create if not running
    return Studio(
        name=name,
        machine=Machine.L40S,
        cloud="lightning-public-prod",
        create_ok=True
    )

def train():
    manifest = load_manifest(DATE)
    train_loader = make_dataloader(manifest, batch_size=8)

    studio = get_or_create_studio()
    if studio.status != "Running":
        studio.start(machine=Machine.L40S)

    model = LightningModule()  # your model impl
    trainer = Trainer(
        max_epochs=1,
        logger=CSVLogger(save_dir="logs"),
        devices=1,
        accelerator="gpu",
        precision="bf16-mixed",
        enable_checkpointing=True
    )
    trainer.fit(model, train_loader)

if __name__ == "__main__":
    train()
```

```bash
# Make launcher executable and set env for cron/daemons
chmod +x /opt/axentx/vanguard/training/train.py
# If used from cron, ensure:
# SHELL=/bin/bash
# 0 2 * * * cd /opt/axentx/vanguard && bash training/train.py >> logs/train.log 2>&1
```

## 4. Verification
1. Run ingestion (or reuse existing) to produce file list for a date folder; call `build_manifest(...)` and confirm `/opt/axentx/vanguard/training/manifests/manifest-YYYY-MM-DD.json` exists and contains `cdn_url` entries.
2. Execute `python -c "from training.manifest import load_manifest; m=load_manifest('YYYY-MM-DD'); print(len(m['entries']), m['entries'][0]['cdn_url'])"` — should print count and a valid CDN URL.
3. Dry-run data loader (no training): 
   ```python
   from training.train import make_dataloader, load_manifest
   loader = make_dataloader(load_manifest("YYYY-MM-DD"), batch_size=2)
   for i, b in enumerate(loader):
       print(i, b["prompt"][:60], b["response"][:60])
       if i >= 3: break
   ```
   Should stream batches without hitting HF `/api/` (check with `tcpdump` or by temporarily blocking `api.huggingface.co` — CDN must still work).
4. Confirm Lightning Studio reuse: run `train()` twice; second run should reuse the running studio (no new studio spin-up) and continue training without quota-wasting recreation.
5. Validate schema resilience: place a malformed parquet (extra columns, missing prompt) in the manifest entries; loader should skip it without crashing.
