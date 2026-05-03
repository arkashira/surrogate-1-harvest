# vanguard / quality

**Final synthesized plan (correctness + concrete actionability)**

**1. Diagnosis (resolved)**
- **Quota burn**: repeated authenticated `list_repo_tree` on every training run causes 429s.
- **No persistence**: missing `(repo, dateFolder) → file-list` manifest forces re-enumeration and prevents reproducible CDN-only training.
- **Mixed access**: training/data-loading likely still uses `load_dataset`/authenticated paths instead of pure CDN fetches.
- **Lightning Studio lifecycle**: no reuse of running instances and no pre-flight/idle checks risk quota waste and mid-run kills.

**2. Core changes (single coherent approach)**
- Add a **manifest generator** that runs once (after quota window clears) and persists a strict JSON manifest.
- Add a **CDN-only loader** that streams Parquet/JSONL via raw HuggingFace CDN URLs (no auth, no API calls).
- Patch the **training entrypoint** to:
  - load the manifest,
  - use the CDN loader,
  - reuse an existing Lightning Studio session when possible,
  - include an idle-timeout guardrail and pre-flight check.
- Keep everything under `/opt/axentx/vanguard/` for consistency and isolation.

**3. Implementation (concrete, ready to run)**

```bash
# One-time setup (run on Mac or where HF token is available)
mkdir -p /opt/axentx/vanguard/{training,manifests,logs}
```

`/opt/axentx/vanguard/training/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate a persisted file manifest for (repo, folder).
Run once per folder after HF API rate-limit window clears.
"""
import json
import os
from pathlib import Path
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_REPO", "datasets/your-org/your-repo")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")  # e.g. batches/mirror-merged/2026-04-29
OUT_ROOT = Path(__file__).parent.parent / "manifests"

def build_manifest(repo: str, folder: str, out_root: Path) -> Path:
    api = HfApi()
    # List only immediate folder contents; paginate automatically by HF SDK
    files = api.list_repo_tree(repo=repo, path=folder.rstrip("/"), recursive=False)
    paths = sorted(
        f.rfilename for f in files
        if f.rfilename.endswith((".parquet", ".jsonl", ".json"))
    )

    safe_name = folder.rstrip("/").replace("/", "_")
    out_dir = out_root / repo.replace("/", "_")
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / f"{safe_name}.json"

    manifest = {
        "repo": repo,
        "folder": folder.rstrip("/"),
        "files": paths,
        "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main"
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {manifest_path} ({len(paths)} files)")
    return manifest_path

if __name__ == "__main__":
    build_manifest(HF_REPO, DATE_FOLDER, OUT_ROOT)
```

`/opt/axentx/vanguard/training/cdn_loader.py`
```python
#!/usr/bin/env python3
"""
CDN-only dataset loader. Uses a pre-generated manifest to avoid HF API calls.
"""
import json
import os
from pathlib import Path
from typing import Iterator, Dict, Any, List, Optional
import pyarrow.parquet as pq
import requests
from io import BytesIO

class CDNParquetLoader:
    def __init__(self, manifest_path: str):
        manifest = json.loads(Path(manifest_path).read_text())
        self.repo = manifest["repo"]
        self.folder = manifest["folder"]
        self.files: List[str] = manifest["files"]
        self.cdn_base = manifest["cdn_base"].rstrip("/")
        if not self.files:
            raise ValueError(f"No parquet/jsonl/json files found in manifest: {manifest_path}")

    def _stream_parquet(self, cdn_url: str) -> pq.Table:
        resp = requests.get(cdn_url, timeout=120)
        resp.raise_for_status()
        return pq.read_table(BytesIO(resp.content))

    def iter_rows(self, columns: Optional[List[str]] = None) -> Iterator[Dict[str, Any]]:
        for fname in self.files:
            cdn_url = f"{self.cdn_base}/{fname}"
            table = self._stream_parquet(cdn_url)
            if columns:
                missing = set(columns) - set(table.column_names)
                if missing:
                    raise KeyError(f"Columns missing in {fname}: {missing}")
                table = table.select(columns)
            for batch in table.to_batches():
                cols = {c: batch.column(c).to_pylist() for c in batch.column_names}
                n = len(batch)
                for i in range(n):
                    yield {k: cols[k][i] for k in cols}

class CDNJsonLinesLoader:
    def __init__(self, manifest_path: str):
        manifest = json.loads(Path(manifest_path).read_text())
        self.repo = manifest["repo"]
        self.folder = manifest["folder"]
        self.files: List[str] = [
            f for f in manifest["files"] if f.endswith((".jsonl", ".json"))
        ]
        self.cdn_base = manifest["cdn_base"].rstrip("/")
        if not self.files:
            raise ValueError(f"No jsonl/json files found in manifest: {manifest_path}")

    def iter_lines(self) -> Iterator[Dict[str, Any]]:
        for fname in self.files:
            cdn_url = f"{self.cdn_base}/{fname}"
            resp = requests.get(cdn_url, timeout=120)
            resp.raise_for_status()
            text = resp.text
            for line in text.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
```

`/opt/axentx/vanguard/training/entrypoint.py`
```python
#!/usr/bin/env python3
"""
Training entrypoint wired to use manifest + CDN loader.
Includes lightweight Lightning Studio lifecycle guardrails.
"""
import os
import sys
import time
import json
import socket
from pathlib import Path
from typing import Optional

# Add local training dir to path
sys.path.insert(0, str(Path(__file__).parent))

from cdn_loader import CDNParquetLoader, CDNJsonLinesLoader

MANIFEST_ROOT = Path(__file__).parent.parent / "manifests"
IDLE_TIMEOUT_SECONDS = int(os.getenv("IDLE_TIMEOUT_SECONDS", "3600"))  # 1h default
STUDIO_HEALTH_FILE = Path(os.getenv("STUDIO_HEALTH_FILE", "/tmp/studio_health.json"))

def _is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

def wait_for_studio(port: int = 8080, max_wait: int = 300, interval: int = 5) -> bool:
    """Wait for an existing Lightning Studio HTTP port to become responsive."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if _is_port_open("localhost", port):
            return True
        time.sleep(interval)
    return False

def record_heartbeat() -> None:
    STUDIO_HEALTH_FILE.parent.mkdir(exist_ok=True, parents=True)
    STUDIO_HEALTH_FILE.write_text(json.dumps({"last_seen": time.time()}))

def studio_idle_too_long(timeout: int = IDLE_TIMEOUT_SECONDS) -> bool:
    if not STUDIO_HEALTH_FILE.exists():
        return False
    try:
        data = json.loads(STUDIO_HEALTH_FILE.read_text())
        last = float(data.get("last_seen", 0))
        return (time.time() - last) > timeout
    except Exception:
        return False

def select_manifest(repo: str, folder: str) -> Path:
    safe_name = folder.rstrip("/").replace("/", "_")
    manifest_path = MANIFEST_ROOT / repo.replace("/", "_") / f"{safe_name}.json"

