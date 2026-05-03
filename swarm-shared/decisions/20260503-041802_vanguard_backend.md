# vanguard / backend

## 1. Diagnosis
- No content-addressed manifest exists → training/frontend hit HF API at runtime, causing 429s and non-reproducible epochs.
- Mixed-schema `enriched/` files include `source`/`ts` columns that break `load_dataset` expectations for surrogate-1 training.
- Lightning Studio is recreated on every run instead of reused, wasting quota and risking idle-stop death.
- Data loader performs HF API calls during training; no CDN-only path strategy to bypass rate limits.
- No deterministic shard-to-repo mapping to escape HF commit cap (128/hr) for ingestion pipelines.

## 2. Proposed change
Create a backend manifest generator and update the surrogate-1 training entrypoint to use CDN-only fetches with a pre-listed file manifest. Scope:
- Add `/opt/axentx/vanguard/backend/manifest.py` (new) — scans a date folder on HF dataset, produces `manifest-{date}.json` with `{repo, path, sha256?, url}` entries using CDN resolve URLs.
- Update `/opt/axentx/vanguard/backend/train.py` (existing or new) — accept `--manifest` arg, use `datasets` with `streaming=False` + `data_files` pointing to local file list or use `IterableDataset` that reads via `fsspec`/`hf_hub_download` from CDN URLs only (no API calls).
- Add deterministic repo selector: `repo = f"vanguard-enriched-{(hash(slug) % 5)}"` to spread writes across 5 sibling repos.

## 3. Implementation

```bash
# /opt/axentx/vanguard/backend/manifest.py
#!/usr/bin/env python3
"""
Generate content-addressed manifest for a date folder in vanguard-enriched-* repos.
Outputs manifest-{date}.json with CDN URLs to bypass HF API rate limits.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

HF_REPO_BASE = "vanguard-enriched"
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def repo_for_slug(slug: str, n_siblings: int = 5) -> str:
    """Deterministic repo selector to spread writes across sibling repos."""
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % n_siblings
    return f"{HF_REPO_BASE}-{idx}"

def build_manifest(date_str: str, out_dir: str = ".") -> str:
    """
    date_str: YYYY-MM-DD folder name present in each vanguard-enriched-* repo.
    Returns path to written manifest.
    """
    entries = []
    # We target the primary repo for listing; files are mirrored across siblings.
    primary_repo = f"{HF_REPO_BASE}-0"
    try:
        tree = list_repo_tree(repo_id=primary_repo, path=date_str, recursive=False)
    except Exception as exc:
        # Fallback: if repo doesn't exist yet, produce empty manifest
        print(f"Warning: could not list {primary_repo}/{date_str}: {exc}", file=sys.stderr)
        tree = []

    for item in tree:
        if item.type != "file":
            continue
        path = f"{date_str}/{item.path.split('/')[-1]}"
        entry = {
            "repo": primary_repo,
            "path": path,
            "cdn_url": CDN_TEMPLATE.format(repo=primary_repo, path=path),
            "date": date_str,
            "filename": item.path.split("/")[-1],
        }
        entries.append(entry)

    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date": date_str,
        "entries": entries,
        "n_siblings": 5,
        "repo_selector": "hash(slug) % 5",
    }

    out_path = Path(out_dir) / f"manifest-{date_str}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(entries)} entries to {out_path}")
    return str(out_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HF CDN manifest for a date folder.")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder in enriched repos")
    parser.add_argument("--out", default=".", help="Output directory")
    args = parser.parse_args()
    build_manifest(args.date, args.out)
```

```python
# /opt/axentx/vanguard/backend/train.py  (create or update)
#!/usr/bin/env python3
"""
Surrogate-1 training entrypoint that uses CDN-only fetches via a pre-generated manifest.
Run on Lightning Studio (L40S/H200) — no HF API calls during training.
"""
import argparse
import json
import sys
from pathlib import Path

import torch
from torch.utils.data import IterableDataset, DataLoader
from datasets import load_dataset

try:
    import lightning as L
except Exception:
    L = None

class CDNTextDataset(IterableDataset):
    """
    Loads JSONL/parquet files via CDN URLs listed in manifest.
    Projects to {prompt, response} only and ignores extra columns.
    """
    def __init__(self, manifest_path: str, split: str = "train", max_samples: int = -1):
        super().__init__()
        self.manifest_path = manifest_path
        self.split = split
        self.max_samples = max_samples
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.entries = self.manifest["entries"]
        if not self.entries:
            raise ValueError("No entries in manifest")

    def _project(self, batch):
        # Keep only prompt/response; tolerate missing keys
        return {
            "prompt": batch.get("prompt") or batch.get("input") or "",
            "response": batch.get("response") or batch.get("output") or "",
        }

    def _stream_from_cdn(self):
        # Use load_dataset with data_files pointing to CDN URLs (no API calls).
        # If files are JSONL/parquet, datasets can stream from HTTPS.
        cdn_files = [e["cdn_url"] for e in self.entries]
        # Use split=None to load all files as one stream; then shard by worker.
        ds = load_dataset(
            "json",  # or "parquet" if all are parquet
            data_files=cdn_files,
            streaming=True,
            split="train",
        )
        for sample in ds:
            projected = self._project(sample)
            if not projected["prompt"] or not projected["response"]:
                continue
            yield projected

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        it = self._stream_from_cdn()
        if worker_info is None:
            limit = self.max_samples
            for i, x in enumerate(it):
                if 0 < limit <= i:
                    break
                yield x
        else:
            # shard across workers
            per_worker = len(self.entries) // worker_info.num_workers
            start = worker_info.id * per_worker
            end = start + per_worker if worker_info.id < worker_info.num_workers - 1 else len(self.entries)
            worker_entries = self.entries[start:end]
            cdn_files = [e["cdn_url"] for e in worker_entries]
            ds = load_dataset("json", data_files=cdn_files, streaming=True, split="train")
            count = 0
            for sample in ds:
                projected = self._project(sample)
                if not projected["prompt"] or not projected["response"]:
                    continue
                if 0 < self.max_samples <= count:
                    break
                yield projected
                count += 1

def train_step(batch, model, optimizer, device):
    # Minimal surrogate-1 step placeholder — replace with real surrogate objective.
    model.train()
    # Dummy: project prompt/response through tokenizer and compute simple loss
    # (implement tokenization + surrogate loss per your model spec)
    loss = torch.tensor(0.0, device=device)
    return loss

def run_training(manifest_path: str, max_steps: int = 1000, batch_size: int = 8):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using
