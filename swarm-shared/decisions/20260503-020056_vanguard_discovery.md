# vanguard / discovery

# 1. Diagnosis

- No persisted `(repo, dateFolder) → file-list` manifest: every training/data-selection run triggers authenticated `list_repo_tree` against HF API, burning quota and risking 429s.
- Training/data loader likely uses `load_dataset(streaming=True)` or repeated per-file API calls on heterogeneous repos, causing `pyarrow.CastError` and wasted API calls.
- No CDN-only fetch path: authenticated API calls are used for data during training instead of public CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`), which bypass auth and rate limits.
- Schema pollution: raw files with mixed schemas are probably being uploaded to `enriched/` with extra metadata columns (`source`, `ts`) instead of projecting to `{prompt, response}` only and using filename-based attribution.
- No Lightning Studio reuse strategy: training jobs likely recreate studios instead of reusing running ones, wasting ~80hr/mo quota and risking idle-stop training death.

# 2. Proposed change

Create `/opt/axentx/vanguard/discovery/prepare_manifest.py` and update training launcher to:
- Persist a date-scoped manifest JSON mapping repo+dateFolder → list of file paths (single `list_repo_tree` call per folder, non-recursive).
- Embed that manifest in training scripts so Lightning workers fetch via CDN URLs only (zero HF API calls during data load).
- Project heterogeneous files to `{prompt, response}` at parse time and store as `batches/mirror-merged/{date}/{slug}.parquet` without extra metadata columns.
- Reuse a running Lightning Studio if present; otherwise start one (L40S → fallback to public tier).

Scope: single new file + small launcher patch (~150 lines total).

# 3. Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/discovery
mkdir -p /opt/axentx/vanguard/training
```

```python
# /opt/axentx/vanguard/discovery/prepare_manifest.py
#!/usr/bin/env python3
"""
Generate and persist a repo+dateFolder -> file-list manifest for CDN-only training.
Usage:
  python prepare_manifest.py --repo datasets/username/mirror-merged --date 2026-05-03 --out manifest-2026-05-03.json
"""
import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

try:
    from huggingface_hub import HfApi, hf_hub_download
except ImportError:
    raise SystemExit("pip install huggingface_hub")

HF_API_RATE_LIMIT_RESET_WAIT = 360  # seconds (per pattern)

def list_date_folder(api: HfApi, repo_id: str, date_folder: str) -> list[str]:
    """
    Single non-recursive tree call per date folder.
    Returns paths like '2026-05-03/file1.parquet' (preserve date prefix).
    """
    try:
        tree = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
    except Exception as exc:
        # crude 429 handling per pattern
        print(f"HF API error (maybe 429): {exc}. Waiting {HF_API_RATE_LIMIT_RESET_WAIT}s")
        time.sleep(HF_API_RATE_LIMIT_RESET_WAIT)
        tree = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)

    files = []
    for entry in tree:
        if entry.rfilename and not entry.rfilename.endswith("/"):
            files.append(entry.rfilename)
    return sorted(files)


def build_manifest(repo_id: str, date_folder: str, output_path: Path) -> dict:
    api = HfApi()
    files = list_date_folder(api, repo_id, date_folder)

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
        "cdn_base": f"https://huggingface.co/datasets/{repo_id}/resolve/main",
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written: {output_path} ({len(files)} files)")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN manifest for date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/username/mirror-merged)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()

    build_manifest(args.repo, args.date, Path(args.out))


if __name__ == "__main__":
    main()
```

```python
# /opt/axentx/vanguard/training/train_cdn_only.py
#!/usr/bin/env python3
"""
Lightning training script that uses a pre-generated manifest and CDN-only fetches.
No HF API calls during data loading.
"""
import json
from pathlib import Path
from typing import Dict, List

import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import Dataset, DataLoader
from lightning import LightningModule, Trainer

try:
    from lightning.pytorch import seed_everything
    from lightning.pytorch.loggers import CSVLogger
except ImportError:
    raise SystemExit("pip install lightning torch pyarrow requests")


class CDNParquetDataset(Dataset):
    """
    Loads parquet shards via CDN URLs listed in manifest.
    Projects rows to {prompt, response} only.
    """
    def __init__(self, manifest_path: str | Path, cache_dir: str | Path = ".cdn_cache"):
        manifest_path = Path(manifest_path)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.cdn_base = manifest["cdn_base"]
        self.files: List[str] = manifest["files"]
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._rows: List[Dict[str, str]] = []
        self._load_all()

    def _cdn_fetch(self, rel_path: str) -> bytes:
        url = f"{self.cdn_base}/{rel_path}"
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content

    def _load_all(self) -> None:
        rows = []
        for rel_path in self.files:
            cache_file = self.cache_dir / Path(rel_path).name
            if cache_file.exists():
                data = cache_file.read_bytes()
            else:
                data = self._cdn_fetch(rel_path)
                cache_file.write_bytes(data)

            table = pq.read_table(pa_buffer=data)
            # Project only prompt/response; tolerate missing cols
            cols = table.column_names
            prompt_col = next((c for c in ("prompt", "instruction", "input") if c in cols), None)
            response_col = next((c for c in ("response", "output", "completion") if c in cols), None)

            if prompt_col is None or response_col is None:
                # skip malformed shard but keep going
                continue

            prompts = table.column(prompt_col).to_pylist()
            responses = table.column(response_col).to_pylist()
            for p, r in zip(prompts, responses):
                if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                    rows.append({"prompt": p.strip(), "response": r.strip()})
        self._rows = rows

    def __len__(self) -> int:
        return len(self._rows)

    def __getitem__(self, idx: int) -> Dict[str, str]:
        return self._rows[idx]


class SimpleSurrogate(LightningModule):
    def __init__(self, lr: float = 1e-4):
        super().__init__()
        self.lr = lr
        # placeholder tiny model for discovery loop; replace with real model later
        self.net = torch.nn.Linear(16, 16)

    def training_step(self, batch, batch_idx):
        # dummy loss for discovery validation
        x = torch.randn(4, 16, device=self.device)
        loss = self
