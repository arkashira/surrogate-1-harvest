# vanguard / discovery

### Final synthesized implementation (strongest + correct + actionable)

**Diagnosis (merged, corrected)**
- No persisted manifest per `(repo, dateFolder)` causes every training launch to re-enumerate via authenticated HF API → burns quota and risks 429.
- Frontend cannot pre-flight or cache available files; users pick invalid/mismatched paths and training fails late.
- Training scripts use `load_dataset(streaming=True)` on heterogeneous repos, which can trigger pyarrow `CastError` on mixed schemas.
- Lightning Studio reuse is not enforced; idle-stop kills training and new runs recreate studios, wasting quota.
- Authenticated API calls continue during training data loading despite CDN-only URLs being available.

**Proposed change (merged scope)**
- Add a manifest builder that runs once per `(repo, dateFolder)` (or on-demand) and persists `manifests/{repo_slug}/{dateFolder}.json` containing file paths + CDN URLs.
- Update training to accept a manifest path and use CDN-only fetches (no HF API calls during training).
- Add pre-flight validation and deterministic Lightning Studio reuse.
- Keep orchestrator changes minimal: invoke manifest build before training launch.

---

### 1. Manifest builder (single source of truth)

File: `/opt/axentx/vanguard/scripts/build_manifest.py`

```python
#!/usr/bin/env python3
"""
Build a file manifest for (repo, dateFolder) using a single HF API call.
Persists manifests/{repo_slug}/{dateFolder}.json for CDN-only training.
"""
import argparse
import json
import os
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import HfApi, list_repo_tree
except ImportError:
    raise RuntimeError("Missing huggingface_hub. Install with: pip install huggingface_hub")

HF_REPO_DEFAULT = "datasets/axentx/surrogate-1"
MANIFEST_ROOT = Path(__file__).resolve().parents[2] / "manifests"

def build_manifest(repo: str, date_folder: str, out_root: Path = MANIFEST_ROOT) -> Path:
    api = HfApi()
    prefix = f"{date_folder}/"

    # Single API call: non-recursive to avoid pagination explosion
    entries = list_repo_tree(repo=repo, path=prefix, recursive=False)

    files: List[Dict[str, str]] = []
    for e in entries:
        if e.type == "file":
            cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{e.path}"
            files.append({"path": e.path, "cdn_url": cdn_url})

    if not files:
        raise ValueError(f"No files found for repo={repo}, date_folder={date_folder}")

    out_root.mkdir(parents=True, exist_ok=True)
    safe_repo = repo.replace("/", "_")
    manifest_path = out_root / safe_repo / f"{date_folder}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "files": files,
        "count": len(files),
    }

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"Manifest written: {manifest_path} ({len(files)} files)")
    return manifest_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build HF file manifest for CDN-only training.")
    parser.add_argument("--repo", default=HF_REPO_DEFAULT, help="HF dataset repo (e.g., datasets/axentx/surrogate-1)")
    parser.add_argument("--date-folder", required=True, help="Date folder in repo (e.g., 2026-04-29)")
    args = parser.parse_args()
    build_manifest(args.repo, args.date_folder)
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

---

### 2. Training loader (CDN-only, robust, no HF API during training)

File: `/opt/axentx/vanguard/training/train.py` (minimal excerpt to embed/replace loader)

```python
import json
import random
import threading
from pathlib import Path
from typing import List, Dict, Optional

import torch
from torch.utils.data import IterableDataset
import requests
from requests.adapters import HTTPAdapter, Retry

# Session per worker with retries and timeouts
_SESSION_LOCAL = threading.local()

def _get_session() -> requests.Session:
    if not hasattr(_SESSION_LOCAL, "session"):
        sess = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        sess.mount("https://", HTTPAdapter(max_retries=retries, pool_connections=8, pool_maxsize=8))
        _SESSION_LOCAL.session = sess
    return _SESSION_LOCAL.session

class CDNTextDataset(IterableDataset):
    """
    CDN-only text dataset using manifest.
    Avoids HF API calls during training and sidesteps pyarrow CastError from mixed schemas.
    """

    def __init__(
        self,
        manifest_path: str,
        seed: int = 42,
        max_retries: int = 3,
        timeout: float = 10.0,
        buffer_size: int = 1024 * 1024,
    ):
        super().__init__()
        self.manifest_path = Path(manifest_path)
        with open(self.manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        self.files: List[Dict[str, str]] = manifest["files"]
        if not self.files:
            raise ValueError(f"No files in manifest: {self.manifest_path}")

        self.seed = seed
        self.max_retries = max_retries
        self.timeout = timeout
        self.buffer_size = buffer_size
        self._shuffle()

    def _shuffle(self) -> None:
        # Deterministic shuffle per worker init
        rng = random.Random(self.seed)
        rng.shuffle(self.files)

    def _fetch_lines(self, cdn_url: str):
        session = _get_session()
        for attempt in range(self.max_retries):
            try:
                with session.get(cdn_url, timeout=self.timeout, stream=True) as r:
                    r.raise_for_status()
                    # Stream decode line-by-line to avoid large memory spikes
                    for chunk in r.iter_lines(decode_unicode=True):
                        if chunk is not None:
                            line = chunk.strip()
                            if line:
                                yield line
                return
            except Exception as exc:
                if attempt == self.max_retries - 1:
                    raise RuntimeError(f"Failed to fetch {cdn_url} after {self.max_retries} attempts") from exc

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is not None:
            # Per-worker deterministic split
            per_worker = len(self.files) // worker_info.num_workers
            start = worker_info.id * per_worker
            end = start + per_worker if worker_info.id < worker_info.num_workers - 1 else len(self.files)
            files = self.files[start:end]
            # Per-worker seed variation
            seed = self.seed + worker_info.id
            rng = random.Random(seed)
            rng.shuffle(files)
        else:
            files = self.files

        for item in files:
            cdn_url = item["cdn_url"]
            try:
                for line in self._fetch_lines(cdn_url):
                    yield line
            except Exception as exc:
                # Fail fast on manifest-backed training to avoid silent data loss
                raise RuntimeError(f"Data loading failed for {cdn_url}") from exc
```

Usage in training script:
```python
manifest_path = "manifests/datasets_axentx_surrogate-1/2026-04-29.json"
train_dataset = CDNTextDataset(manifest_path=manifest_path, seed=42)
train_loader = torch.utils.data.DataLoader(
    train_dataset,
    batch_size=None,  # yields raw lines/tokens; collate in tokenizer
    num_workers=4,
    pin_memory=True,
)
```

---

### 3. Pre-flight validation (run before training launch)

File: `/opt/axentx/vanguard/scripts/p
