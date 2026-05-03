# vanguard / discovery

## Final Synthesis (single, actionable plan)

**Core problem**: training and UI hit the HF API at runtime (429s, non-reproducible runs), ingestion writes mixed-schema files, and there is no deterministic, shareable snapshot.  
**Goal**: one deterministic, CDN-only path from ingestion → manifest → training with zero HF API calls during training and reproducible snapshots.

---

### 1. High-level design (merged)

- **Ingestion** projects every file to `{prompt, response}` and writes deterministic parquet to  
  `batches/mirror-merged/{date}/{slug}.parquet`.
- **Manifest** is content-addressed:  
  `manifests/{date}-{sha256(file_list)}.json` containing CDN URLs, per-file hash/size, and a deterministic snapshot ID.
- **Training** uses a `load_cdn_only(manifest)` helper that fetches via public CDN URLs (no HF API, no auth headers).
- **Orchestration guardrails**:
  - Pre-flight check for running Lightning Studio instances to avoid quota waste and collisions.
  - Optional lockfile to serialize ingestion/training runs and prevent concurrent writes to the same date folder.
- **Reproducibility**: manifest pins exact file list and hashes; training loads only from CDN; no `streaming=True` on heterogeneous files.

---

### 2. File layout (concrete)

```
/opt/axentx/vanguard/
├── batches/
│   └── mirror-merged/
│       └── {date}/
│           └── {slug}.parquet          # prompt/response only
├── manifests/
│   └── {date}-{sha256(file_list)}.json
├── raw/
│   └── {date}/                         # optional staging for raw inputs
├── scripts/
│   └── studio_guard.py                 # pre-flight + lock helpers
├── ingest.py
├── manifest.py
└── train.py
```

---

### 3. Implementation (merged + hardened)

#### manifest.py
```python
import json, hashlib, os
from pathlib import Path
from typing import List, Dict
from datetime import datetime, timezone

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

def file_list_hash(file_infos: List[Dict]) -> str:
    payload = json.dumps(file_infos, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]

def build_manifest(repo: str, date_folder: str, file_infos: List[Dict]) -> Path:
    """
    file_infos: [{"path": "batches/mirror-merged/2026-05-03/x.parquet", "size": 1234, "etag": "abc..."}]
    """
    h = file_list_hash(file_infos)
    manifest = {
        "repo": repo,
        "date": date_folder,
        "snapshot": h,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": file_infos,
        "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main",
        "created_by": "vanguard-ingest"
    }
    out = MANIFEST_DIR / f"{date_folder}-{h}.json"
    out.write_text(json.dumps(manifest, indent=2))
    return out

def load_manifest(path: Path) -> Dict:
    return json.loads(path.read_text())

def cdn_urls(manifest: Dict):
    base = manifest["cdn_base"]
    for f in manifest["files"]:
        yield f"{base}/{f['path']}"
```

#### scripts/studio_guard.py
```python
import os, fcntl, time, subprocess
from pathlib import Path

LOCK_DIR = Path(__file__).parent.parent / ".locks"
LOCK_DIR.mkdir(exist_ok=True)

def running_studio_instances():
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "lightning"], stderr=subprocess.DEVNULL
        ).decode().strip().split()
        return [int(p) for p in out if p]
    except subprocess.CalledProcessError:
        return []

def wait_for_studio(timeout: int = 300, poll: int = 15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not running_studio_instances():
            return True
        time.sleep(poll)
    return False

def run_lock(name: str):
    lock_file = LOCK_DIR / f"{name}.lock"
    lock_file.parent.mkdir(exist_ok=True)
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError(f"Another run holds lock: {name}")
```

#### ingest.py
```python
#!/usr/bin/env python3
import os, pyarrow.parquet as pq, pyarrow as pa
from pathlib import Path
from manifest import build_manifest
from scripts.studio_guard import run_lock, wait_for_studio

HF_REPO = os.getenv("HF_REPO", "org/vanguard-data")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-05-03")
BASE = Path(__file__).parent
OUT_DIR = BASE / "batches" / "mirror-merged" / DATE_FOLDER
RAW_DIR = BASE / "raw" / DATE_FOLDER

def project_to_prompt_response(raw_path: Path):
    tbl = pq.read_table(raw_path)
    cols = {c.lower().strip(): c for c in tbl.column_names}
    prompt_col = cols.get("prompt") or cols.get("instruction") or cols.get("input")
    response_col = cols.get("response") or cols.get("output") or cols.get("completion")
    if not prompt_col or not response_col:
        raise ValueError(f"Missing prompt/response in {raw_path}: {tbl.column_names}")
    return pa.table({"prompt": tbl[prompt_col], "response": tbl[response_col]})

def main():
    # Guardrails
    if not wait_for_studio():
        raise RuntimeError("Lightning Studio still running after timeout; aborting.")
    lock = run_lock(f"ingest-{DATE_FOLDER}")

    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        if not RAW_DIR.exists():
            print("No raw files; skipping.")
            return

        file_infos = []
        for raw_file in RAW_DIR.glob("*.parquet"):
            try:
                tbl = project_to_prompt_response(raw_file)
                slug = raw_file.stem
                out_file = OUT_DIR / f"{slug}.parquet"
                pq.write_table(tbl, out_file)
                file_infos.append({
                    "path": str(out_file.relative_to(BASE.parent)),
                    "size": out_file.stat().st_size,
                    "etag": ""  # populate via hf_hub_download etag if available
                })
            except Exception as e:
                print(f"Skip {raw_file}: {e}")

        manifest_path = build_manifest(HF_REPO, DATE_FOLDER, file_infos)
        print(f"Manifest: {manifest_path}")
    finally:
        try:
            os.close(lock)
        except Exception:
            pass

if __name__ == "__main__":
    main()
```

#### train.py (CDN-only loader + deterministic split)
```python
#!/usr/bin/env python3
import json, io, random, argparse
from pathlib import Path
import pyarrow.parquet as pq
import pyarrow as pa
import requests
from manifest import load_manifest, cdn_urls

def load_cdn_only(manifest_path: Path):
    manifest = load_manifest(manifest_path)
    rows = []
    for url in cdn_urls(manifest):
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        tbl = pq.read_table(io.BytesIO(resp.content))
        tbl = tbl.select(["prompt", "response"])
        rows.append(tbl)
    return pa.concat_tables(rows)

def deterministic_split(table, train_frac=0.99, seed=42):
    n = table.num_rows
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    split = int(n * train_frac)
    train_idx = sorted(idx[:split])

