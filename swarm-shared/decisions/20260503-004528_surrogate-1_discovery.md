# surrogate-1 / discovery

## Final Unified Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs, and a training script that uses **CDN-only** fetches (zero HF API calls during training). This implements the CDN bypass pattern, avoids HF API rate limits, and enables Lightning Studio reuse with robust idle-stop and fallback behavior.

---

### Steps (1h 30m total)

1. **Create tools/snapshot_manifest.py** (20m)  
   - Single `list_repo_tree` call for `batches/public-merged/<date>/` (non-recursive)  
   - Emit CDN URLs: `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/...`  
   - Output `file_manifest.json` with `{cdn_url, path, size, sha256_if_available}`  
   - Accept date arg (default today) and repo override; fail fast on API errors so operator can retry after window.

2. **Create training/data_loader_cdn.py** (25m)  
   - Read `file_manifest.json`; stream each file from CDN via `urllib.request` (no auth)  
   - Parse JSONL lines with resilient schema projection to `{prompt, response}`  
   - Provide `IterableDataset` and optional deterministic subsampling for quick smoke tests.

3. **Create training/train_cdn.py** (30m)  
   - Lightning `LightningModule` stub with real tokenizer/loss hook points  
   - Studio reuse logic: list running studios, attach if exists, else create with L40S fallback  
   - Idle-stop guard: check status before `.run()`, restart if stopped; graceful shutdown on interrupt  
   - Deterministic shard selection via hash-slug → repo mapping (5-sibling spread) if needed.

4. **Update README.md** (10m)  
   - Add “CDN training” section with usage:  
     ```bash
     python tools/snapshot_manifest.py --date 2026-05-03 --out manifest.json
     python training/train_cdn.py --manifest manifest.json --studio surrogate-1-l40s
     ```

5. **Smoke test** (15m)  
   - Run snapshot_manifest locally (Mac) → verify manifest  
   - Dry-run data loader with first 10 files → verify projection  
   - Push to repo.

---

## tools/snapshot_manifest.py

```python
#!/usr/bin/env python3
"""
snapshot_manifest.py
List one date-partition of axentx/surrogate-1-training-pairs via a single
HF API call and emit file_manifest.json with CDN URLs for zero-auth fetches.

Usage:
    python snapshot_manifest.py --date 2026-05-03 --out manifest.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi, list_repo_tree

REPO = "axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/datasets/{REPO}/resolve/main"

def snapshot(date_str: str, out_path: Path, repo: str = REPO) -> None:
    """
    date_str: YYYY-MM-DD
    """
    prefix = f"batches/public-merged/{date_str}/"
    api = HfApi()

    # Single non-recursive call
    try:
        tree = list_repo_tree(repo=repo, path=prefix, recursive=False)
    except Exception as exc:
        print(f"HF API error (possible 429): {exc}", file=sys.stderr)
        sys.exit(1)

    files = []
    for entry in tree:
        if entry.type != "file":
            continue
        cdn_url = f"{CDN_ROOT}/{entry.path}"
        files.append(
            {
                "path": entry.path,
                "cdn_url": cdn_url,
                "size": getattr(entry, "size", None),
                "sha256": getattr(entry, "lfs", {}).get("sha256")
                if hasattr(entry, "lfs")
                else None,
            }
        )

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "date_partition": date_str,
        "prefix": prefix,
        "repo": repo,
        "file_count": len(files),
        "files": files,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files -> {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create CDN manifest for one date partition.")
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"), help="YYYY-MM-DD")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    parser.add_argument("--repo", default=REPO, help="HF dataset repo")
    args = parser.parse_args()

    snapshot(args.date, Path(args.out), args.repo)
```

---

## training/data_loader_cdn.py

```python
import json
import urllib.request
from typing import Dict, Iterator, Optional

def stream_cdn_jsonl(url: str, max_lines: Optional[int] = None) -> Iterator[Dict]:
    """Stream a JSONL file from CDN and yield parsed dicts."""
    with urllib.request.urlopen(url) as resp:
        for i, line in enumerate(resp):
            if max_lines is not None and i >= max_lines:
                break
            line = line.decode("utf-8").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield obj

def project_prompt_response(obj: Dict) -> Dict:
    """
    Normalize heterogeneous schemas to {prompt, response}.
    Adjust heuristics here as new schemas appear.
    """
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
    response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def iter_dataset_from_manifest(manifest_path: str, max_lines_per_file: Optional[int] = None) -> Iterator[Dict]:
    with open(manifest_path) as f:
        manifest = json.load(f)

    for file_info in manifest["files"]:
        url = file_info["cdn_url"]
        for raw in stream_cdn_jsonl(url, max_lines=max_lines_per_file):
            yield project_prompt_response(raw)

class CDNIterableDataset:
    """Lightning-friendly iterable dataset wrapper."""
    def __init__(self, manifest_path: str, max_lines_per_file: Optional[int] = None):
        self.manifest_path = manifest_path
        self.max_lines_per_file = max_lines_per_file

    def __iter__(self):
        return iter_dataset_from_manifest(self.manifest_path, self.max_lines_per_file)
```

---

## training/train_cdn.py

```python
#!/usr/bin/env python3
import argparse
import signal
import sys
from pathlib import Path

import torch
from torch.utils.data import DataLoader
from lightning import LightningModule, Trainer
from lightning.pytorch.callbacks import ModelCheckpoint

from data_loader_cdn import CDNIterableDataset, project_prompt_response

# ----- Studio imports (best-effort) -----
try:
    from lightning import Studio
    _STUDIO_AVAILABLE = True
except Exception:
    _STUDIO_AVAILABLE = False
    Studio = None

# ----- Model stub (replace with real tokenizer + transformer) -----
class Surrogate1Model(LightningModule):
    def __init__(self, lr: float = 1e-4, vocab_size: int = 50257, d_model: int = 1024):
        super().__init__()
        self.save_hyperparameters()
        self.lr = lr
        # Minimal stub; replace with real model/tokenizer
        self.net = torch.nn.Linear(d_model, d_model)
        self.loss_fn = torch.nn.CrossEntropyLoss()

    def training_step(self, batch, batch_idx):
        # Real
