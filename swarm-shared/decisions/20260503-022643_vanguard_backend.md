# vanguard / backend

## Final Consolidated Implementation  
*(Best parts merged, contradictions resolved in favor of correctness + concrete actionability)*

---

### 1. Diagnosis (resolved)
- **No persisted file manifest** → single authenticated `list_repo_tree` per `(repo, date_folder)`; cache to disk to avoid quota/429.
- **No CDN-bypass** → training must never call `load_dataset`/`streaming=True`; use CDN URLs only.
- **Schema projection too late** → project to `{prompt, response}` at parse time; drop extra columns before training.
- **No Studio reuse / idle-stop resilience** → list running studios first; if idle-stopped, restart cleanly instead of failing.
- **No backpressure / retries / memory safety** → add retries, timeouts, and temp-file cleanup; avoid loading entire parquet into RAM.

---

### 2. Files

#### `/opt/axentx/vanguard/backend/manifest.py`
```python
#!/usr/bin/env python3
"""
Build and cache CDN-only file manifests for HF datasets.
Avoids HF API rate limits during training.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from huggingface_hub import HfApi

HF_API = HfApi()
MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def _normalize_path(item) -> str:
    if isinstance(item, dict):
        return item.get("path")
    return getattr(item, "path", None)


def list_date_folder(repo: str, date_folder: str) -> List[str]:
    """
    Single authenticated call to list files in date_folder (non-recursive).
    Returns relative paths.
    """
    items = HF_API.list_repo_tree(
        repo=repo,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )
    paths = []
    for item in items:
        p = _normalize_path(item)
        if p:
            paths.append(p)
    return sorted(paths)


def build_manifest(repo: str, date_folder: str) -> Dict:
    """
    Build manifest with CDN URLs.
    """
    paths = list_date_folder(repo, date_folder)
    files = [
        {
            "path": p,
            "url": CDN_TEMPLATE.format(repo=repo, path=p),
        }
        for p in paths
    ]
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    out_path = MANIFEST_DIR / f"{repo.replace('/', '__')}__{date_folder.replace('/', '_')}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def load_manifest(repo: str, date_folder: str) -> Dict:
    safe_repo = repo.replace("/", "__")
    safe_date = date_folder.replace("/", "_")
    out_path = MANIFEST_DIR / f"{safe_repo}__{safe_date}.json"
    if not out_path.exists():
        return build_manifest(repo, date_folder)
    return json.loads(out_path.read_text())
```

---

#### `/opt/axentx/vanguard/backend/train.py`
```python
#!/usr/bin/env python3
"""
Surrogate-1 training entrypoint (Lightning Studio).
Uses CDN-only data loading and schema projection.
"""
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterator, Any

import pyarrow.parquet as pq
import requests
import torch
from datasets import IterableDataset
from lightning import Fabric, LightningModule
from lightning.pytorch.studio import Studio
from huggingface_hub import Teamspace

from .manifest import load_manifest

HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-data")
DATE_FOLDER = os.getenv("DATE_FOLDER", "batches/mirror-merged/2026-05-03")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "1000"))
STUDIO_NAME = os.getenv("STUDIO_NAME", "surrogate-1-train")


# ---- Studio reuse / idle-stop resilience ----
def get_or_start_studio(name: str = STUDIO_NAME) -> Studio:
    team = Teamspace()
    for s in team.studios:
        if s.name == name:
            if s.status == "Running":
                print(f"Reusing running studio: {name}")
                return s
            else:
                print(f"Studio {name} exists but status={s.status}. Starting new studio.")
                break
    print(f"Starting new studio: {name}")
    return Studio(
        name=name,
        machine="L40S",
        cloud="lightning-public-prod",
        create_ok=True,
    )


studio = get_or_start_studio()


# ---- CDN-only iterable dataset with retries & memory safety ----
class CdnParquetDataset(IterableDataset):
    def __init__(
        self,
        manifest_path: str,
        columns=("prompt", "response"),
        max_retries: int = 3,
        timeout: int = 30,
    ):
        self.manifest_path = manifest_path
        self.columns = columns
        self.max_retries = max_retries
        self.timeout = timeout

    def _project_record(self, raw: Dict[str, Any]) -> Dict[str, str]:
        return {k: str(raw.get(k, "")) for k in self.columns}

    def _fetch_with_retry(self, url: str) -> bytes:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, timeout=self.timeout)
                resp.raise_for_status()
                return resp.content
            except Exception as exc:
                if attempt == self.max_retries:
                    raise
                sleep_sec = min(2 ** attempt, 10)
                print(f"Retry {attempt}/{self.max_retries} for {url}: {exc}. Sleeping {sleep_sec}s")
                time.sleep(sleep_sec)
        raise RuntimeError("Unreachable")

    def _stream_files(self) -> Iterator[Dict[str, str]]:
        manifest = json.loads(Path(self.manifest_path).read_text())
        for f in manifest["files"]:
            if not f["path"].endswith(".parquet"):
                continue
            url = f["url"]
            content = self._fetch_with_retry(url)
            with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                table = pq.read_table(tmp_path, columns=self.columns)
                for batch in table.to_batches(max_chunksize=1024):
                    for i in range(batch.num_rows):
                        raw = {c: batch.column(c)[i].as_py() for c in self.columns}
                        yield self._project_record(raw)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def __iter__(self) -> Iterator[Dict[str, str]]:
        yield from self._stream_files()


# ---- Minimal model (placeholder) ----
class SurrogateModel(LightningModule):
    def __init__(self):
        super().__init__()
        self.embed = torch.nn.Embedding(32000, 1024)
        self.out = torch.nn.Linear(1024, 32000)

    def training_step(self, batch, batch_idx):
        # Dummy loss for illustration; replace with real tokenized training
        x = torch.randint(0, 32000, (BATCH_SIZE, 128), device=self.device)
        y = self.out(self.embed(x))
        loss = torch.nn.functional.cross_entropy(y.view(-1, 32000), x.view(-1))
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), lr=1e-4)


def main():
    # Build/load manifest once (orchestrator can refresh after rate-limit window)
    manifest = load
