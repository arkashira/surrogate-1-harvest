# airship / discovery

## Highest-Value Incremental Improvement (<2h)

**Goal**: Eliminate HF API 429s during Surrogate training and make Lightning resilient to idle-stop so iteration is **<2 minutes** and never blocked by rate limits.

**Why this wins**:  
- Directly fixes the Surrogate training pipeline (HF 429 + idle-stop kills).  
- Uses the CDN-bypass pattern already validated (no auth, no rate limit).  
- Single-file change + small orchestration tweak → ships in <2h with zero breaking changes.

---

## Implementation Plan

1. **Add CDN-only file fetcher**  
   - Replace any `load_dataset(..., streaming=True)` or HF API data loader with a CDN downloader that uses `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth).  
   - Accept a local `file_list.json` produced once by the Mac orchestration script (after rate-limit window clears) to avoid recursive `list_repo_files`.

2. **Make Lightning Studio resilient to idle-stop**  
   - Before each `.run()`, check studio status; if stopped, restart with `target.start(machine=Machine.L40S)` (or fallback to free-tier machine).  
   - Reuse running studios to preserve quota.

3. **Wire into Surrogate training entrypoint**  
   - Update the training launcher to:
     - Load `file_list.json`.
     - Use CDN fetcher for parquet rows.
     - Project to `{prompt, response}` only at parse time.
   - Ensure no `source`/`ts` columns are added to enriched outputs (filename carries attribution).

4. **Validation**  
   - Run a quick smoke train on 100 files via Lightning Studio and confirm zero HF API calls during data load (check logs for `resolve/main` URLs and no 429s).

---

## Code Snippets

### `surrogate/cdn_data.py` (new)
```python
import json
import os
from pathlib import Path
from typing import List, Dict, Any

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

HF_DATASETS_BASE = "https://huggingface.co/datasets"

def load_file_list(file_list_path: str) -> List[str]:
    with open(file_list_path) as f:
        return json.load(f)

def cdn_download_file(repo: str, file_path: str, local_path: Path) -> Path:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{HF_DATASETS_BASE}/{repo}/resolve/main/{file_path}"
    if local_path.exists():
        return local_path
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    return local_path

def stream_parquet_rows_from_cdn(
    repo: str,
    file_list: List[str],
    cache_dir: str = ".cdn_cache",
    max_files: int = None,
) -> List[Dict[str, Any]]:
    cache_dir = Path(cache_dir)
    rows = []
    files = file_list[:max_files] if max_files else file_list
    for rel_path in tqdm(files, desc="CDN fetch"):
        local_file = cdn_download_file(repo, rel_path, cache_dir / rel_path)
        try:
            table = pq.read_table(local_file)
            # Project to minimal schema at parse time
            batch = table.select(["prompt", "response"]).to_pylist()
            rows.extend(batch)
        except Exception as exc:
            # Skip malformed/heterogeneous files; log and continue
            print(f"Skipping {rel_path}: {exc}")
            continue
    return rows
```

### `surrogate/lightning_studio.py` (resilient launcher)
```python
from lightning_sdk import Studio, Machine, Teamspace
from typing import Optional

def get_or_start_studio(
    name: str,
    machine: Machine = Machine.L40S,
    fallback_machine: Machine = Machine.L40S,  # adjust per free-tier if needed
) -> Studio:
    studios = Teamspace.studios()
    running = [s for s in studios if s.name == name and s.status == "Running"]
    if running:
        return running[0]

    stopped = [s for s in studios if s.name == name and s.status == "Stopped"]
    if stopped:
        studio = stopped[0]
        try:
            studio.start(machine=machine)
            return studio
        except Exception:
            # If target machine unavailable (e.g., H200 not in free account), fallback
            studio.start(machine=fallback_machine)
            return studio

    # Create new only if necessary
    studio = Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )
    return studio

def run_training_with_resilience(
    script_path: str,
    studio_name: str = "surrogate-train",
    machine: Machine = Machine.L40S,
    fallback_machine: Machine = Machine.L40S,
) -> None:
    studio = get_or_start_studio(studio_name, machine=machine, fallback_machine=fallback_machine)
    if studio.status != "Running":
        raise RuntimeError(f"Studio {studio_name} not running after start attempt")
    # Lightweight run; adapt CLI as needed
    studio.run(
        run_name="surrogate-cdn-train",
        command=f"python {script_path}",
        wait=False,
    )
```

### Mac orchestration snippet (produce `file_list.json` once)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="org/surrogate-data"
DATE_DIR="2026-05-03"
OUT="file_list.json"

# Single API call (after rate-limit window) — non-recursive per folder if needed
python -c "
import json
from huggingface_hub import list_repo_tree
files = [f.rfilename for f in list_repo_tree('$REPO', path='$DATE_DIR', recursive=True)]
json.dump(files, open('$OUT', 'w'), indent=2)
"
echo "Saved $(wc -l < $OUT) files to $OUT"
```

### Update training entrypoint to use CDN
In your main train script (or Surrogate training module), replace dataset loading with:
```python
from surrogate.cdn_data import load_file_list, stream_parquet_rows_from_cdn

file_list = load_file_list("file_list.json")
rows = stream_parquet_rows_from_cdn("org/surrogate-data", file_list, max_files=None)
# rows -> list of {prompt, response}
```

---

## Acceptance Criteria

- [ ] Zero HF API calls (429s) during training data load (logs show only `resolve/main` CDN URLs).  
- [ ] Lightning Studio auto-restarts if stopped (idle-stop resilience).  
- [ ] Training completes on 100-file sample without schema errors (heterogeneous files skipped safely).  
- [ ] No new columns (`source`, `ts`) added to enriched outputs; attribution via filename pattern preserved.
