# airship / frontend

## Analysis & Plan

**Highest-value incremental improvement (<2h):**  
Add a CDN-only dataset loader + Lightning idle-resilient runner to `/opt/axentx/airship/surrogate` so training:
- Never calls HF Hub API during training (bypasses 429 rate limits)
- Auto-restarts if Lightning Studio stops (idle timeout resilience)
- Writes to sibling repos deterministically when pushing artifacts

**Implementation plan:**
1. Create `surrogate/cdn_dataset.py` — pre-list files once, embed JSON, stream via CDN (no auth)
2. Create `surrogate/lightning_runner.py` — idle-aware studio reuse, auto-restart, deterministic sibling repo selector
3. Add `surrogate/train_cdn.py` — minimal training loop using CDN dataset
4. Update `surrogate/requirements.txt` if needed (requests, pyarrow, datasets)
5. Verify with quick smoke test (dry-run imports + file listing)

---

## Code

### 1) `surrogate/cdn_dataset.py`
```python
"""
CDN-only dataset loader.
- list_repo_tree() called once (from dev machine) -> file_list.json
- Lightning training uses ONLY CDN URLs (no HF API/auth/429)
- Projects heterogeneous files to {prompt, response} at parse time
"""
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


class CDNDataset:
    def __init__(
        self,
        repo: str,
        file_list_path: str,
        split: str = "train",
        columns: tuple = ("prompt", "response"),
        cache_dir: Optional[str] = None,
    ):
        self.repo = repo
        self.columns = columns
        self.cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".cache" / "surrogate_cdn"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        with open(file_list_path) as f:
            self.files: List[str] = json.load(f)  # e.g. ["batches/mirror-merged/2026-04-29/a.parquet", ...]

        if split != "train":
            # simple prefix split; adapt as needed
            self.files = [f for f in self.files if split in f]

    def _cdn_url(self, path: str) -> str:
        return HF_CDN_TEMPLATE.format(repo=self.repo, path=path)

    def _download_cached(self, path: str) -> Path:
        url = self._cdn_url(path)
        cache_file = self.cache_dir / path.replace("/", "_")
        if cache_file.exists():
            return cache_file

        cache_file.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        with open(cache_file, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return cache_file

    def stream_parquet_rows(self, max_files: Optional[int] = None):
        """
        Yield dict rows with only self.columns.
        Handles mixed-schema parquet files by projection at parse time.
        """
        count = 0
        for rel_path in self.files:
            if max_files is not None and count >= max_files:
                break

            try:
                local_path = self._download_cached(rel_path)
                table = pq.read_table(local_path, columns=self.columns)
            except Exception as exc:
                # skip malformed / missing columns
                print(f"Skipping {rel_path}: {exc}")
                continue

            df = table.to_pandas()
            for _, row in df.iterrows():
                yield {k: row.get(k, "") for k in self.columns}
                count += 1

    def to_parquet(self, output_path: str, max_files: Optional[int] = None):
        """
        Convert CDN dataset to a single local parquet (for Lightning quick loads).
        """
        rows = list(self.stream_parquet_rows(max_files=max_files))
        if not rows:
            raise ValueError("No rows extracted.")

        table = pa.Table.from_pylist(rows)
        pq.write_table(table, output_path)
        return output_path
```

### 2) `surrogate/lightning_runner.py`
```python
"""
Lightning Studio runner with idle-resilience and deterministic sibling-repo writes.
- Reuse running studios (quota friendly)
- Auto-restart if idle-stopped before .run()
- Deterministic sibling repo selector for artifact pushes
"""
import hashlib
import os
import subprocess
import sys
from typing import List, Optional

try:
    from lightning_sdk import Client, Machine
    from lightning_sdk.workspace import Teamspace
except ImportError:
    print("lightning-sdk not installed; install via `pip install lightning` if running on Mac orchestrator.")
    Client = None
    Teamspace = None


def _hash_slug_to_index(slug: str, n_siblings: int = 5) -> int:
    """Deterministic sibling index from slug."""
    digest = hashlib.sha256(slug.encode()).hexdigest()
    return int(digest, 16) % n_siblings


class LightningRunner:
    def __init__(
        self,
        teamspace: str = "default",
        cloud_priority: Optional[List[str]] = None,
        machine_size: str = "L40S",
        sibling_repos: Optional[List[str]] = None,
    ):
        self.teamspace = teamspace
        self.cloud_priority = cloud_priority or [
            "lightning-lambda-prod",  # H200 available
            "lightning-public-prod",  # L40S max on free tier
        ]
        self.machine_size = machine_size
        self.sibling_repos = sibling_repos or []
        self.client = Client() if Client else None

    def get_or_create_studio(self, name: str, script_path: str, requirements: Optional[str] = None):
        if self.client is None:
            raise RuntimeError("lightning-sdk unavailable; run on orchestrator with lightning installed.")

        ts = Teamspace(name=self.teamspace, client=self.client)

        # Reuse running studio
        for s in ts.studios:
            if s.name == name and s.status == "running":
                print(f"Reusing running studio: {name}")
                return s

        # If stopped, restart with target machine
        for s in ts.studios:
            if s.name == name and s.status == "stopped":
                print(f"Restarting stopped studio: {name}")
                s.start(machine=Machine(self.machine_size, cloud=self.cloud_priority[0]))
                return s

        # Create new
        print(f"Creating studio: {name}")
        return ts.studios.create(
            name=name,
            script_path=script_path,
            machine=Machine(self.machine_size, cloud=self.cloud_priority[0]),
            requirements=requirements or "requirements.txt",
        )

    def run_with_idle_resilience(self, name: str, script_path: str, max_retries: int = 3):
        """
        Run script in studio; if idle-stop detected, restart and retry.
        """
        for attempt in range(1, max_retries + 1):
            try:
                studio = self.get_or_create_studio(name=name, script_path=script_path)
                # studio.run will fail if idle-stopped; we catch and restart
                result = studio.run(script_path)
                print(f"Studio run completed: {result}")
                return result
            except Exception as exc:
                print(f"Attempt {attempt}/{max_retries} failed: {exc}")
                if attempt < max_retries:
                    print("Restarting studio and retrying...")
                    try:
                        ts = Teamspace(name=self.teamspace, client=self.client)
                        for s in ts.studios:
                            if s.name == name:
                                s.start(machine=Machine(self.machine_size, cloud=self.cloud_priority[0]))
                                break
                    except Exception as inner:
                        print(f"Restart failed: {inner}")
                else:
                    raise

    def pick_sibling_repo(self, slug: str) -> Optional[str]:
        if not self.sibling_repos:
            return None
        idx = _hash_slug_to_index(slug, n_siblings=len(self.sibling_re
