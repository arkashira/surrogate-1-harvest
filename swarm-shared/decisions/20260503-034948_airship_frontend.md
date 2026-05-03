# airship / frontend

## Final Implementation Plan (≤2h)

**Highest-value change**: Add CDN-only dataset loader + Lightning idle-resilient runner to `/opt/axentx/airship/surrogate` so training never hits HF API during data loading and survives Lightning idle stops.

### Concrete steps
1. Create `surrogate/cdn_loader.py` — single JSON file list → CDN-only streaming loader (zero HF API calls during training).
2. Create `surrogate/lightning_runner.py` — idle-resilient runner that checks studio status, restarts L40S→H200 fallback, and reuses running studios.
3. Add small util `surrogate/utils.py` for repo-to-sibling deterministic routing and file-list caching.
4. Update `surrogate/requirements.txt` (if present) or document deps (`lightning`, `datasets`, `pyarrow`, `requests`).
5. Add `surrogate/train_cdn.py` minimal entrypoint that wires CDN loader into surrogate training loop (can be invoked by runner).

Total estimated time: 90–110 minutes.

---

## surrogate/cdn_loader.py
```python
"""
CDN-only dataset loader for HuggingFace public datasets.

Usage:
    from surrogate.cdn_loader import get_cdn_dataset

    dataset = get_cdn_dataset(
        repo="my-org/surrogate-data",
        folder="batches/mirror-merged/2026-05-03",
        file_list_path="file_list.json",   # optional; if missing, one-time Mac API call
        streaming=True
    )
"""

import json
import os
from typing import Dict, Iterator, List, Optional
from urllib.parse import quote

import pyarrow.parquet as pq
import requests
from datasets import IterableDataset, DatasetDict


HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def build_cdn_url(repo: str, path: str) -> str:
    return HF_CDN_TEMPLATE.format(repo=repo, path=quote(path, safe=""))


def load_file_list(file_list_path: str) -> List[str]:
    with open(file_list_path, "r") as f:
        data = json.load(f)
    if isinstance(data, dict) and "files" in data:
        return data["files"]
    return data


def cdn_parquet_reader(paths: List[str], repo: str, columns: Optional[List[str]] = None):
    """
    Yield rows from parquet files via CDN URLs.
    Uses pyarrow memory-map-friendly fetch via requests + streaming bytes.
    """
    for rel_path in paths:
        url = build_cdn_url(repo, rel_path)
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        # read into memory (parquet requires seekable; small shards OK)
        data = resp.content
        table = pq.read_table(pq.ParquetFile(pq.ParquetDataset(pq.ParquetDataset(pq.ParquetDataset(data))))).to_pylist()
        for row in table:
            if columns:
                row = {k: row.get(k) for k in columns}
            yield row


def get_cdn_dataset(
    repo: str,
    folder: str,
    file_list_path: Optional[str] = None,
    streaming: bool = True,
    columns: Optional[List[str]] = None,
) -> IterableDataset:
    """
    Return an IterableDataset that reads only from CDN.
    file_list_path: JSON produced by Mac orchestration script:
        {"files": ["batches/mirror-merged/2026-05-03/xxx.parquet", ...]}
    If file_list_path is missing, caller must provide paths via folder enumeration
    (not implemented here to avoid HF API; orchestration script must provide list).
    """
    if file_list_path is None or not os.path.exists(file_list_path):
        raise FileNotFoundError(
            "file_list_path required for CDN loader. Run Mac orchestration script "
            "to produce file list (list_repo_tree -> JSON) and embed in training."
        )

    paths = load_file_list(file_list_path)
    # Filter by folder prefix if provided
    if folder:
        paths = [p for p in paths if p.startswith(folder.rstrip("/") + "/")]

    if not paths:
        raise ValueError("No files found for repo={repo} folder={folder}")

    def gen() -> Iterator[Dict]:
        yield from cdn_parquet_reader(paths, repo, columns=columns)

    features = None
    if columns:
        # minimal features; adjust if schema known
        from datasets import Features, Value
        features = Features({c: Value("string") for c in columns})

    ds = IterableDataset.from_generator(gen, features=features)
    return DatasetDict({"train": ds}) if streaming else ds
```

---

## surrogate/lightning_runner.py
```python
"""
Lightning idle-resilient runner for Surrogate training.

- Reuses running studios to save quota.
- Falls back L40S -> H200 with cloud priority.
- Restarts studio if stopped before run.
"""

import time
from typing import Optional

from lightning import LightningWork, LightningFlow, LightningApp, Machine
from lightning.app import BuildConfig
from lightning.app.utilities.app_helpers import Logger

logger = Logger(__name__)


# Cloud priority: paid H200 first (lightning-lambda-prod), then free-tier L40S
CLOUD_MACHINE_OPTIONS = [
    {"cloud": "lightning-lambda-prod", "machine": "H200"},
    {"cloud": "lightning-public-prod", "machine": "L40S"},
]


class SurrogateTrainer(LightningWork):
    def __init__(
        self,
        repo: str,
        folder: str,
        file_list_path: str,
        script_path: str = "surrogate/train_cdn.py",
        max_retries: int = 3,
        restart_on_idle: bool = True,
        **work_kwargs,
    ):
        super().__init__(
            cloud_build_config=BuildConfig(requirements=["pyarrow", "requests", "datasets"]),
            **work_kwargs,
        )
        self.repo = repo
        self.folder = folder
        self.file_list_path = file_list_path
        self.script_path = script_path
        self.max_retries = max_retries
        self.restart_on_idle = restart_on_idle
        self._current_machine_index = 0

    def _current_machine(self) -> Machine:
        opts = CLOUD_MACHINE_OPTIONS[self._current_machine_index]
        return Machine(
            cloud=opts["cloud"],
            name=opts["machine"],
        )

    def _pick_next_machine(self):
        self._current_machine_index = (self._current_machine_index + 1) % len(CLOUD_MACHINE_OPTIONS)

    def run(self):
        from lightning import Teamspace

        studio_name = f"surrogate-train-{self.repo.replace('/', '-')}"
        teamspace = Teamspace()

        # Reuse running studio if available
        running = None
        for s in teamspace.studios:
            if s.name == studio_name and s.status == "running":
                running = s
                break

        if running:
            logger.info(f"Reusing running studio: {studio_name}")
            studio = running
        else:
            logger.info(f"Creating studio: {studio_name}")
            from lightning import Studio
            studio = Studio(
                name=studio_name,
                machine=self._current_machine(),
                create_ok=True,
            )

        # Ensure studio is running before executing
        if studio.status != "running":
            if not self.restart_on_idle:
                logger.error(f"Studio {studio_name} is not running and restart_on_idle=False")
                return
            logger.info(f"Restarting studio {studio_name} (was {studio.status})")
            studio.start(machine=self._current_machine())
            # Wait for running
            for _ in range(60):
                if studio.status == "running":
                    break
                time.sleep(5)
            if studio.status != "running":
                logger.error(f"Studio failed to start: {studio.status}")
                self._pick_next_machine()
                return

        # Execute training script via studio.run()
        cmd = [
            "python",
            self.script_path,
            "--repo",
            self.repo,
            "--folder",
            self.folder,
            "--file-list",
            self.file_list_path,
        ]

       
