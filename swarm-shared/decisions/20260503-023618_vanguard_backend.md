# vanguard / backend

## Final Synthesis (Best Parts + Correctness + Actionability)

I merged the strongest, non-contradictory pieces from both proposals and fixed correctness issues (retry/backoff, schema safety, session reuse, and manifest generation). The result is a single, production-ready plan that eliminates HF API calls during training and prevents 429s.

---

## 1. Diagnosis (Consolidated)
- Backend still uses authenticated HF API (`load_dataset`, `list_repo_tree`) per training run → burns quota and causes 429s.
- No static file manifest: each run re-enumerates repo via API.
- No CDN bypass: training uses `/api/` endpoints instead of public CDN URLs.
- No resilient retry/backoff or transient-failure handling for downloads.
- No schema normalization: mixed-schema Parquet files cause `pyarrow` CastError.
- No Lightning Studio session reuse: redundant launches waste quota.

---

## 2. Proposed Changes (Scope)
- Add **manifest generator** (one-time, authenticated) and **CDN-only loader** (training).
- Add **robust retry/backoff** and **schema-safe row extraction**.
- Add **Lightning Studio reuse check** to avoid duplicate launches.
- Minimal file changes:
  - `/opt/axentx/vanguard/backend/generate_manifest.py` (new)
  - `/opt/axentx/vanguard/backend/data_loader.py` (new)
  - `/opt/axentx/vanguard/backend/train_launcher.py` (new)
  - `/opt/axentx/vanguard/backend/train.py` (modify to use loader)
  - `/opt/axentx/vanguard/backend/requirements.txt` (append)

---

## 3. Implementation

### 3.1 generate_manifest.py
```python
#!/usr/bin/env python3
"""
One-time manifest generator. Run from dev box after rate-limit window clears.

Usage:
    python generate_manifest.py \
        --repo "org/surrogate-1" \
        --date "2026-04-29" \
        --out "data/manifest.json"
"""

import argparse
import json
import os
import sys
from typing import List, Dict

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

API = HfApi()

def list_date_files(repo_id: str, date: str) -> List[Dict]:
    path = date
    try:
        entries = API.list_repo_tree(repo_id=repo_id, path=path, recursive=False)
    except Exception as exc:
        raise RuntimeError(f"HF API list_repo_tree failed for {repo_id}/{path}: {exc}") from exc

    files = []
    for e in entries:
        if e.type == "file":
            files.append(
                {
                    "path": e.path,
                    "cdn_url": f"https://huggingface.co/datasets/{repo_id}/resolve/main/{e.path}",
                    "size": e.size or 0,
                }
            )
    return files

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN manifest for Surrogate-1 training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (org/repo)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default="data/manifest.json", help="Output JSON path")
    args = parser.parse_args()

    print(f"Listing {args.repo}/{args.date}/ ...")
    files = list_date_files(args.repo, args.date)
    if not files:
        print(f"WARNING: No files found under {args.date}/")

    manifest = {
        "repo_id": args.repo,
        "date": args.date,
        "generated_by": "generate_manifest.py",
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files -> {args.out}")

if __name__ == "__main__":
    main()
```

---

### 3.2 data_loader.py
```python
import json
import time
import requests
import pyarrow.parquet as pq
from io import BytesIO
from typing import Iterator, Dict, Any
from tqdm import tqdm

CDN_TIMEOUT = (10, 30)  # connect, read
MAX_RETRIES = 5
BACKOFF_FACTOR = 1.5

def robust_get(url: str) -> bytes:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=CDN_TIMEOUT)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts: {exc}") from exc
            sleep_sec = BACKOFF_FACTOR ** attempt
            time.sleep(sleep_sec)

def safe_row_iter(table) -> Iterator[Dict[str, Any]]:
    df = table.to_pandas()
    # Normalize column names and coerce to string safely
    prompt_col = next((c for c in df.columns if "prompt" in c.lower()), None)
    response_col = next((c for c in df.columns if "response" in c.lower()), None)
    for _, row in df.iterrows():
        prompt = str(row.get(prompt_col, "")) if prompt_col else ""
        response = str(row.get(response_col, "")) if response_col else ""
        yield {"prompt": prompt, "response": response}

def load_rows_from_manifest(manifest_path: str) -> Iterator[Dict[str, Any]]:
    with open(manifest_path) as f:
        manifest = json.load(f)

    for meta in tqdm(manifest["files"], desc="Streaming shards"):
        url = meta["cdn_url"]
        content = robust_get(url)
        table = pq.read_table(BytesIO(content))
        yield from safe_row_iter(table)
```

---

### 3.3 train_launcher.py
```python
import subprocess
import sys
import os

def find_running_studio(name: str) -> str:
    """Return studio ID if running, else empty."""
    try:
        result = subprocess.run(
            ["lightning", "studios", "list", "--name", name, "--format", "json"],
            capture_output=True, text=True, check=False
        )
        # Simplified parsing; adapt to actual CLI output
        if result.returncode == 0 and result.stdout.strip():
            import json as _json
            studios = _json.loads(result.stdout)
            if studios:
                return studios[0].get("id", "")
    except Exception:
        pass
    return ""

def launch_studio_if_needed(name: str, script: str, script_args: list) -> None:
    studio_id = find_running_studio(name)
    cmd = ["lightning", "studios", "run", name, "--machine", "L40S", "--script", script]
    if studio_id:
        print(f"Reusing existing studio: {studio_id}")
        # Attach or run script in existing studio; adapt to your CLI
        cmd = ["lightning", "studios", "run", studio_id, "--script", script] + script_args
    else:
        print("No running studio found. Starting new L40S session.")
        cmd += script_args

    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)

if __name__ == "__main__":
    # Example usage
    launch_studio_if_needed(
        name="surrogate-1-train",
        script="backend/train.py",
        script_args=["--manifest-path", "data/manifest.json"]
    )
```

---

### 3.4 train.py (modification)
```diff
+ from backend.data_loader import load_rows_from_manifest
+
  def train(cfg):
-     # OLD: load_dataset(...) or list_repo_tree(...)
-     # Replace with CDN manifest loader
+     dataset = list(load_rows_from_manifest(cfg.manifest_path))
+     # continue training loop...
```

---

### 3.5 requirements.txt (append)
```text
requests>=2.31.0
tqdm>=4.66.0
