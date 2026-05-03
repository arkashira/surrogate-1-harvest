# airship / discovery

## Final Synthesized Implementation (Best of Both Candidates)

**Chosen approach**: Manifest-driven, CDN-only iterable loader that eliminates HF API calls during training and avoids `pyarrow.CastError` from mixed-schema repos.

**Why this wins**:
- Zero HF API calls during training (preserves quota, no 429s)
- Lazy CDN downloads avoid large local storage
- Projection-at-parse isolates only `{prompt, response}` and ignores incompatible files
- Drop-in replacement for `load_dataset` in existing training scripts
- Can be built and verified in ≤2 hours

---

## Concrete Action Plan (Ordered for Execution)

### 1. Build the manifest (one-time per folder)
```bash
python scripts/build_hf_manifest.py \
  --repo surrogate-data \
  --date 2026-04-29 \
  --out manifests/
```

**`scripts/build_hf_manifest.py`**
```python
#!/usr/bin/env python3
"""
Build a CDN-only manifest for a repo+date folder.
Usage:
  python scripts/build_hf_manifest.py --repo surrogate-data --date 2026-04-29 --out manifests/
"""
import argparse
import json
import sys
from pathlib import Path

import requests


def list_repo_tree(repo: str, path: str):
    url = f"https://huggingface.co/api/datasets/{repo}/tree/{path}"
    resp = requests.get(url, timeout=30)
    if resp.status_code == 429:
        print("HF API rate-limited (429). Wait 360s and retry.", file=sys.stderr)
        sys.exit(1)
    resp.raise_for_status()
    return resp.json()


def build_manifest(repo: str, date_folder: str, out_dir: Path):
    tree = list_repo_tree(repo, date_folder)
    manifest = []
    for item in tree:
        if item.get("type") != "file":
            continue
        filename = item["path"]
        manifest.append(
            {
                "filename": filename,
                "size": item.get("size"),
                "sha": item.get("sha"),
                "url": f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{filename}",
            }
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_folder}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path} ({len(manifest)} files)")
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build HF CDN manifest")
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/ds)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    parser.add_argument("--out", default="manifests", help="Output directory")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, Path(args.out))
```

---

### 2. Add the CDN-only dataset loader
**`airship/data/hf_cdn_dataset.py`**
```python
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

import requests
import torch
from torch.utils.data import IterableDataset

logger = logging.getLogger(__name__)


def project_record(raw_bytes: bytes, filename: str) -> Optional[Dict[str, str]]:
    """
    Project raw file bytes into {prompt, response}.
    Supports JSONL lines with prompt/response keys.
    Returns None if projection fails (avoids mixed-schema crashes).
    """
    try:
        text = raw_bytes.decode("utf-8")
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if prompt is not None and response is not None:
                return {"prompt": str(prompt), "response": str(response)}
        logger.warning("No prompt/response found in %s", filename)
        return None
    except Exception as exc:
        logger.warning("Projection failed for %s: %s", filename, exc)
        return None


class HFCDNDataset(IterableDataset):
    """
    CDN-only dataset using a manifest JSON.
    Manifest format: [{"filename": "...", "url": "...", ...}, ...]
    """

    def __init__(self, manifest_path: Path, max_files: Optional[int] = None):
        self.manifest_path = Path(manifest_path)
        with self.manifest_path.open() as f:
            self.manifest: List[Dict] = json.load(f)
        if max_files:
            self.manifest = self.manifest[:max_files]

    def __len__(self):
        return len(self.manifest)

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            iter_slice = self.manifest
        else:
            per_worker = len(self.manifest) // worker_info.num_workers
            worker_id = worker_info.id
            start = per_worker * worker_id
            end = per_worker * (worker_id + 1) if worker_id < worker_info.num_workers - 1 else len(self.manifest)
            iter_slice = self.manifest[start:end]

        for item in iter_slice:
            url = item["url"]
            filename = item["filename"]
            try:
                resp = requests.get(url, stream=True, timeout=30)
                resp.raise_for_status()
                raw = resp.content
                record = project_record(raw, filename)
                if record is not None:
                    yield record
            except Exception as exc:
                logger.error("Failed to fetch %s: %s", url, exc)
                continue
```

---

### 3. Wire into training (Lightning-ready)
**`examples/train_surrogate.py`**
```python
import argparse
from pathlib import Path

import lightning as L
import torch
from torch.utils.data import DataLoader

from airship.data.hf_cdn_dataset import HFCDNDataset


class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, manifest_path: Path, batch_size: int = 4, max_files: int = None):
        super().__init__()
        self.manifest_path = manifest_path
        self.batch_size = batch_size
        self.max_files = max_files

    def train_dataloader(self):
        dataset = HFCDNDataset(self.manifest_path, max_files=self.max_files)
        return DataLoader(dataset, batch_size=self.batch_size, num_workers=2)


class SurrogateTrainer(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.model = torch.nn.Linear(1024, 1024)
        self.loss = torch.nn.MSELoss()

    def training_step(self, batch, batch_idx):
        # Replace with real model/tokenizer logic
        prompts = batch["prompt"]
        responses = batch["response"]
        # Dummy forward for smoke test
        dummy = torch.randn(len(prompts), 1024)
        loss = self.loss(self.model(dummy), dummy)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--limit-train-batches", type=int, default=10)
    args = parser.parse_args()

    dm = SurrogateDataModule(
        manifest_path=args.manifest,
        batch_size=args.batch_size,
        max_files=args.max_files,
    )
    model = SurrogateTrainer()
    trainer = L.Trainer(
        max_epochs=1,
        limit_train_batches=args.limit_train_batches,
        accelerator="gpu",
        devices=1,
    )
    trainer.fit(model, dm)
```

---

### 
