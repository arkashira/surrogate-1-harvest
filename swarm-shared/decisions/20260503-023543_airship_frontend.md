# airship / frontend

## Final Implementation Plan — CDN-first Surrogate Training (Mac + Lightning)

**Scope**: `/opt/axentx/airship/surrogate` ingestion + training path  
**Value**: eliminates HF API 429s during training; stops Lightning quota waste by reusing running Studios; deterministic, CDN-only fetches; retry/backoff for CDN 429s; reproducible local cache.

---

### 1) High-level steps (≤2h)

1. **Mac-side file listing** (run once per date folder after HF rate-limit window)  
   - `scripts/list_hf_date_folder.py` → writes `data/file_list.json` (deterministic, sorted).

2. **CDN-only DataLoader** (Lightning-side)  
   - `surrogate/data/cdn_loader.py` reads `file_list.json`, downloads via `resolve/main/...` URLs with `requests`/`aiohttp` + streaming + retries.  
   - Supports sequential and threaded download; returns local file paths.

3. **Lightning Studio reuse** (avoid repeated quota burn)  
   - `surrogate/training/lightning_reuse.py` finds running Studio by name; if stopped, restarts; otherwise creates with `L40S` in `lightning-public-prod`.

4. **Update training entrypoint** (`train.py`)  
   - Accept `--file-list`, `--out-dir`, `--studio-name`.  
   - Use `cdn_loader` to materialize local cache before training.  
   - Call `lightning_reuse` to get running Studio, then launch training.

5. **Robustness**  
   - Exponential backoff for CDN 429s/5xx (max retries configurable).  
   - Skip already-downloaded files (content-addressed by filename; deterministic listing).  
   - `requirements-cdn.txt` with `requests`, `aiohttp`, `tqdm`.

---

### 2) Code (production-ready)

#### `scripts/list_hf_date_folder.py`
```python
#!/usr/bin/env python3
"""
List HF dataset files for one date folder (non-recursive) and save to JSON.
Run from Mac after HF API rate-limit window clears.

Usage:
  python scripts/list_hf_date_folder.py \
    --repo surrogate-dataset \
    --date 2026-04-29 \
    --out data/file_list.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., surrogate-dataset)")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-04-29)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN", None), help="HF token (optional for public repos)")
    args = parser.parse_args()

    api = HfApi(token=args.token)
    folder = f"{args.date}/"
    try:
        tree = api.list_repo_tree(repo_id=args.repo, path=folder, recursive=False)
    except Exception as e:
        print(f"Error listing repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    files = [
        item.rfilename
        for item in tree
        if item.rfilename.startswith(folder) and not item.rfilename.endswith("/")
    ]
    files = sorted(set(files))

    payload = {
        "repo": args.repo,
        "date": args.date,
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

#### `surrogate/data/cdn_loader.py`
```python
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import requests
from tqdm import tqdm

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def load_file_list(file_list_path: str) -> Dict[str, object]:
    with open(file_list_path) as f:
        return json.load(f)

def download_via_cdn(
    repo: str,
    path: str,
    out_dir: str,
    max_retries: int = 5,
    backoff: float = 2.0,
    timeout: int = 60,
) -> str:
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    out_path = os.path.join(out_dir, os.path.basename(path))
    if os.path.exists(out_path):
        return out_path

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, stream=True, timeout=timeout)
            resp.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            return out_path
        except Exception as exc:
            if attempt == max_retries:
                raise RuntimeError(f"Failed to download {url} after {max_retries} attempts") from exc
            sleep_sec = backoff * (2 ** (attempt - 1))
            print(f"CDN download failed ({exc}), retry {attempt}/{max_retries} in {sleep_sec:.1f}s: {url}")
            time.sleep(sleep_sec)
    raise RuntimeError("unreachable")

def build_local_dataset(
    file_list_path: str,
    out_dir: str,
    max_workers: int = 4,
    max_retries: int = 5,
    backoff: float = 2.0,
) -> List[str]:
    """
    Download files listed in file_list.json to out_dir and return local paths.
    Uses ThreadPoolExecutor for concurrent CDN downloads.
    """
    meta = load_file_list(file_list_path)
    repo = meta["repo"]
    files = meta["files"]
    local_paths = [None] * len(files)

    def _download(idx_path):
        idx, p = idx_path
        return idx, download_via_cdn(repo, p, out_dir, max_retries=max_retries, backoff=backoff)

    with ThreadPoolExecutor(max_workers=max_workers) as ex, tqdm(
        total=len(files), desc="Downloading via CDN"
    ) as pbar:
        futures = {ex.submit(_download, (i, p)): i for i, p in enumerate(files)}
        for future in as_completed(futures):
            idx, local_path = future.result()
            local_paths[idx] = local_path
            pbar.update(1)

    # Safety check
    assert all(lp is not None for lp in local_paths), "Some downloads failed"
    return local_paths
```

#### `surrogate/training/lightning_reuse.py`
```python
import os
from typing import Optional

from lightning import L40S, Machine, Studio, Teamspace

def get_or_create_studio(
    name: str,
    script: str,
    cloud: str = "lightning-public-prod",
    machine: Machine = L40S,
    dependencies: Optional[list] = None,
) -> Studio:
    """
    Reuse a running Studio with `name` if exists; otherwise create one.
    Avoids quota waste from repeated studio creation.
    """
    for s in Teamspace.studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {name} (id={s.id})")
            return s

    print(f"No running studio '{name}' found. Creating...")
    studio = Studio(
        name=name,
        script=script,
        cloud=cloud,
        machine=machine,
        dependencies=dependencies or [],
        create_ok=True,
    )
    return studio

def ensure_running(studio: Studio, machine: Machine = L40S) -> Studio:
    """
    If studio is stopped, restart it. Lightning idle stop kills training.
    """
    if studio.status != "running":
        print(f"Studio {studio.name}
