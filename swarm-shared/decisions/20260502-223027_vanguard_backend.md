# vanguard / backend

## Final Synthesized Implementation

**Core diagnosis (accepted from both candidates):**  
- No canonical entrypoint → ad-hoc planning, violates `#knowledge-rag #graph #hub`.  
- No HF CDN-bypass file list → surrogate-1 training will hit 429s.  
- No Lightning Studio reuse guard → quota waste and idle-stop failures.  
- No ingestion hygiene for mixed-schema HF repos → `pyarrow.CastError` risk.  
- No hardened wrapper script → cron/daemon failures (`#bash #script-error`).

**Chosen solution (highest leverage, minimal footprint):**  
Create one package root, one orchestrator, one hygienic ingestion helper, and one hardened wrapper. All changes are additive and non-breaking.

---

### 1) Create structure and package marker
```bash
mkdir -p /opt/axentx/vanguard/backend/batches/mirror-merged
touch /opt/axentx/vanguard/backend/__init__.py
```

---

### 2) `/opt/axentx/vanguard/backend/orchestrate.py`
Single entrypoint that:
- Generates CDN-bypass file list once per date folder (non-recursive `list_repo_tree`).
- Reuses a running Lightning Studio by name; starts/resumes if stopped.
- Runs a training script inside the studio with zero API calls during data loading.

```python
from __future__ import annotations
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lightning as L

HF_REPO = os.getenv("HF_REPO", "datasets/example/mirror")
HF_DATE = os.getenv("HF_DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
STUDIO_NAME = os.getenv("STUDIO_NAME", "vanguard-surrogate-train")
MACHINE = os.getenv("MACHINE", "L40S")

def list_hf_folder_for_cdn_bypass(repo: str, folder: str) -> list[str]:
    """
    Single API call. Returns CDN-resolvable paths (no Authorization header required).
    """
    from huggingface_hub import HfApi

    api = HfApi()
    tree = api.list_repo_tree(repo, path=folder, recursive=False)
    return [item.rfilename for item in tree if item.type == "file"]

def write_file_list(paths: list[str], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(paths, f, indent=2)

def reuse_or_start_studio(name: str, machine_name: str) -> L.studio.Studio:
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s

    print(f"Starting new studio: {name} on {machine_name}")
    machine = L.Machine(machine_name)
    return L.Studio(name=name, machine=machine, create_ok=True)

def ensure_studio_running(studio: L.studio.Studio, machine_name: str) -> None:
    if studio.status != "Running":
        print(f"Studio stopped; restarting on {machine_name}")
        studio.start(machine=L.Machine(machine_name))

def run_training_in_studio(
    studio: L.studio.Studio,
    script_path: str,
    file_list_path: str,
    env_overrides: dict[str, str] | None = None,
) -> None:
    env = {
        "HF_FILE_LIST": file_list_path,
        "HF_REPO": HF_REPO,
        "HF_DATE": HF_DATE,
    }
    if env_overrides:
        env.update(env_overrides)

    # Training script must use CDN-only fetches (no HF API calls).
    studio.run(target=script_path, env=env)

def main() -> None:
    folder = f"data/{HF_DATE}"
    file_list_path = Path("file_list.json").resolve()

    # 1) Generate CDN file list (run once per date folder from Mac).
    if not file_list_path.exists():
        print(f"Generating CDN file list for {HF_REPO}/{folder}")
        paths = list_hf_folder_for_cdn_bypass(HF_REPO, folder)
        write_file_list(paths, file_list_path)
        print(f"Wrote {len(paths)} files to {file_list_path}")
    else:
        print(f"Using existing file list: {file_list_path}")

    # 2) Reuse or start studio.
    studio = reuse_or_start_studio(STUDIO_NAME, MACHINE)
    ensure_studio_running(studio, MACHINE)

    # 3) Run training script (passed via CLI or default).
    script = sys.argv[1] if len(sys.argv) > 1 else "train.py"
    run_training_in_studio(studio, script, str(file_list_path))

if __name__ == "__main__":
    main()
```

---

### 3) `/opt/axentx/vanguard/backend/ingest_hf.py`
Minimal hygienic ingestion helper that:
- Downloads via CDN URLs (no HF API auth during bulk fetch).
- Projects heterogeneous input schemas to `{prompt, response}`.
- Writes `batches/mirror-merged/{date}/{slug}.parquet`.

```python
from __future__ import annotations
import json
from pathlib import Path
from typing import Iterator

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

CDN_ROOT = "https://huggingface.co/datasets"

def cdn_url(repo: str, filepath: str) -> str:
    return f"{CDN_ROOT}/{repo}/resolve/main/{filepath}"

def iter_cdn_records(repo: str, filepaths: list[str], batch_size: int = 1000) -> Iterator[pa.Table]:
    for fp in tqdm(filepaths, desc="Downloading"):
        url = cdn_url(repo, fp)
        r = requests.get(url, timeout=60)
        r.raise_for_status()

        rows = []
        for line in r.text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            rows.append({
                "prompt": obj.get("prompt") or obj.get("input") or "",
                "response": obj.get("response") or obj.get("output") or "",
            })
        if rows:
            tbl = pa.Table.from_pylist(
                rows,
                schema=pa.schema([
                    pa.field("prompt", pa.string()),
                    pa.field("response", pa.string()),
                ]),
            )
            yield tbl

def write_mirror_parquet(
    repo: str,
    date: str,
    filepaths: list[str],
    out_root: Path | str = "batches/mirror-merged",
) -> Path:
    out_dir = Path(out_root) / date
    out_dir.mkdir(parents=True, exist_ok=True)

    slug = "merged"
    out_path = out_dir / f"{slug}.parquet"

    schema = pa.schema([
        pa.field("prompt", pa.string()),
        pa.field("response", pa.string()),
    ])

    with pq.ParquetWriter(out_path, schema=schema) as writer:
        for tbl in iter_cdn_records(repo, filepaths):
            writer.write_table(tbl)

    return out_path

if __name__ == "__main__":
    import os
    repo = os.getenv("HF_REPO", "datasets/example/mirror")
    date = os.getenv("HF_DATE", "YYYY-MM-DD")
    filepaths = json.loads(Path("file_list.json").read_text())
    out = write_mirror_parquet(repo, date, filepaths)
    print(f"Wrote {out}")
```

---

### 4) `/opt/axentx/vanguard/run_vanguard.sh`
Hard wrapper with shebang, strict mode, and sane defaults.

```bash
#!/usr/bin/env bash
# run_vanguard.sh — hardened orchestration wrapper
# Usage: ./run_vanguard.sh [train_script.py]

set -euo pipefail

cd "$(dirname "$0")/backend"

export HF_REPO="${HF_REPO:-datasets/example/mirror}"
export HF_DATE="${HF_DATE:-$(date -u +%Y-%m-%d)}"
export STUDIO
