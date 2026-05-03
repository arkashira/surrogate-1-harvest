# vanguard / backend

## 1. Diagnosis

- No CDN-first manifest: ingestion/training can still trigger runtime `list_repo_tree`/`load_dataset` calls from backend jobs → 429 risk and non-reproducible runs.
- Missing deterministic, content-addressed file list keyed by `{date}/{slug}` → jobs re-enumerate on every run and can diverge.
- Backend likely uses `load_dataset(..., streaming=True)` on heterogeneous repos → `pyarrow.CastError` on mixed-schema files.
- No fallback to CDN bypass (`resolve/main/...`) when HF API 429s or during training → training stalls on rate limits.
- No studio reuse/idle handling in Lightning launcher → quota waste and idle-timeout kills training.

## 2. Proposed change

Add a CDN-first ingestion manifest generator and deterministic file-listing utility under `vanguard/backend/ingest/`:

- `vanguard/backend/ingest/manifest.py` — single API call to `list_repo_tree` per date folder, produce `manifest-{date}.json` with `{slug, cdn_url, sha256?}` entries.
- `vanguard/backend/ingest/cdn_loader.py` — stream from CDN URLs (no auth), project `{prompt, response}` at parse time, skip mixed-schema columns.
- `vanguard/backend/training/lightning_launcher.py` — reuse running studios, embed manifest path, CDN-only data loader, restart on idle-stop.

Scope: create/modify files only in `/opt/axentx/vanguard/backend/`.

## 3. Implementation

```bash
# Create structure
mkdir -p /opt/axentx/vanguard/backend/{ingest,training,utils}
```

### manifest.py

```python
# /opt/axentx/vanguard/backend/ingest/manifest.py
import json
import os
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import HfApi
except ImportError:
    HfApi = None

MANIFEST_DIR = Path(__file__).parent.parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

def list_date_folder(repo_id: str, date_folder: str, token: str = None) -> List[Dict]:
    """
    Single API call: list one date folder (non-recursive) to avoid pagination/429.
    Returns list of dicts with slug and CDN URL.
    """
    if HfApi is None:
        raise RuntimeError("huggingface_hub not installed")

    api = HfApi(token=token)
    # Use recursive=False to avoid 100x pagination; caller can iterate subfolders if needed.
    files = api.list_repo_tree(repo_id, path=date_folder, recursive=False)

    entries = []
    for f in files:
        if getattr(f, "type", None) != "file":
            continue
        path = f.path
        slug = path.replace("/", "_").replace(".", "_")
        cdn_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
        entries.append({
            "path": path,
            "slug": slug,
            "cdn_url": cdn_url,
            "size": getattr(f, "size", None),
        })

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "entries": entries,
    }
    out_path = MANIFEST_DIR / f"manifest-{date_folder.replace('/', '_')}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest

if __name__ == "__main__":
    import sys
    repo = sys.argv[1] if len(sys.argv) > 1 else "dataset/repo"
    datef = sys.argv[2] if len(sys.argv) > 2 else "batches/mirror-merged/2026-05-03"
    token = os.getenv("HF_TOKEN")
    m = list_date_folder(repo, datef, token=token)
    print(f"Wrote {len(m['entries'])} entries to {MANIFEST_DIR}")
```

### cdn_loader.py

```python
# /opt/axentx/vanguard/backend/ingest/cdn_loader.py
import json
import requests
from typing import Iterator, Dict, Any
import pyarrow as pa
import pyarrow.parquet as pq
from io import BytesIO

def stream_cdn_parquet(cdn_url: str, columns=("prompt", "response")) -> Iterator[Dict[str, Any]]:
    """
    Download single file from CDN (no auth) and project only required columns.
    Avoids pyarrow CastError from mixed-schema files by selecting columns at read time.
    """
    resp = requests.get(cdn_url, timeout=30)
    resp.raise_for_status()
    buf = BytesIO(resp.content)

    try:
        table = pq.read_table(buf, columns=columns)
    except (pa.ArrowInvalid, KeyError, ValueError):
        # Fallback: read all and project
        table = pq.read_table(buf)
        available = set(table.column_names)
        pick = [c for c in columns if c in available]
        if not pick:
            return
        table = table.select(pick)

    for batch in table.to_batches():
        df = batch.to_pandas()
        for _, row in df.iterrows():
            yield {k: row.get(k) for k in columns}

def iter_manifest(manifest_path: str) -> Iterator[Dict[str, Any]]:
    m = json.loads(open(manifest_path).read())
    for e in m["entries"]:
        yield e
```

### lightning_launcher.py

```python
# /opt/axentx/vanguard/backend/training/lightning_launcher.py
import os
import json
from pathlib import Path
from typing import Optional

try:
    from lightning import LightningWork, LightningFlow, LightningApp, Machine
    from lightning.app import Teamspace
except ImportError:
    LightningWork = LightningFlow = LightningApp = Machine = None
    Teamspace = None

MANIFEST_DIR = Path(__file__).parent.parent / "manifests"

def find_running_studio(name: str):
    if Teamspace is None:
        return None
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            return s
    return None

def launch_surrogate_train(
    manifest_name: str,
    script_path: str,
    studio_name: str = "surrogate-train",
    machine: str = "lightning-public-prod://L40S",
):
    """
    Reuse running studio if present; otherwise create.
    Embed manifest path so training uses CDN-only fetches.
    """
    manifest_file = MANIFEST_DIR / manifest_name
    if not manifest_file.exists():
        raise FileNotFoundError(f"Manifest missing: {manifest_file}")

    existing = find_running_studio(studio_name)
    if existing:
        print(f"Reusing running studio: {studio_name}")
        # If stopped, restart
        if hasattr(existing, "stop") and existing.status != "Running":
            existing.start(machine=Machine(machine))
        target = existing
    else:
        if LightningWork is None:
            raise RuntimeError("lightning not installed")
        class TrainWork(LightningWork):
            def run(self, manifest_path: str):
                import subprocess
                cmd = [
                    "bash", script_path,
                    "--manifest", manifest_path,
                    "--cdn-only"
                ]
                subprocess.run(cmd, check=True)

        target = TrainWork(name=studio_name, cloud_compute=Machine(machine))

    # In LightningApp context you would wire flows; for CLI-driven usage:
    # we return the target so caller can start it.
    return target

if __name__ == "__main__":
    import sys
    manifest_n = sys.argv[1] if len(sys.argv) > 1 else "manifest-batches_mirror-merged_2026-05-03.json"
    script = sys.argv[2] if len(sys.argv) > 2 else str(Path(__file__).parent.parent / "train.py")
    launch_surrogate_train(manifest_n, script)
```

### train.py (minimal stub)

```python
# /opt/axentx/vanguard/backend/train.py
import argparse
import json
from pathlib import Path
