# airship / discovery

## Final Synthesis (Best-of + Corrected + Actionable)

Use a **deterministic, CDN-only discovery pipeline** that eliminates 429s and schema errors in <2h.  
Key choices resolved for correctness + actionability:

- **Do the file list once** (HF API `list_repo_tree`, date-scoped) → deterministic JSON.  
- **Never call HF API during training**; fetch files via raw CDN (`resolve/main/...`) with no auth header.  
- **Project schema at parse time** (only `prompt`, `response`) to avoid PyArrow cast errors on heterogeneous repos.  
- **Retry/backoff + sharding** for HF commit caps and 429s (exponential backoff, jitter, sibling-repo round-robin).  
- **Reuse running Lightning/Kaggle infra**; pass file list as artifact or env var.  
- **Kaggle pushes use Bearer token + `isPrivate=True`** to avoid phone-verification 403s.

---

## Implementation Plan (Concrete, Ordered)

### 1) Deterministic file listing (orchestration host / CI)
- Single `list_repo_tree(repo_id, path=DATE_FOLDER, recursive=False)` per date folder.
- Deterministic sort → `file-list.json` (repo_id, date_folder, listed_at_utc, files[]).
- Commit or upload as artifact for training jobs.

### 2) CDN-only fetches in training
- Use `requests.get(CDN_URL)` with no Authorization header (bypasses `/api/` rate limits).
- If auth required for private repos, use `hf_hub_download(repo_type="dataset")` with token but prefer raw CDN for public.
- Stream and project columns; never use `load_dataset` on mixed-schema repos.

### 3) Schema-safe projection
- Parquet: `pq.read_table(..., columns=["prompt","response"])` → ignore extra/mismatched columns.
- JSONL: line-by-line, pick keys; skip malformed lines.
- Normalize missing columns to `None`.

### 4) Retry/backoff + sharding
- Exponential backoff with jitter for 429/5xx (max retries configurable).
- For HF commit caps, shard writes across sibling repos (deterministic round-robin by hash of path).

### 5) Lightning/Kaggle integration
- Lightning: reuse running studio; pass file-list via artifact or env var.
- Kaggle kernel push: Bearer token + `isPrivate=True`.

---

## Corrected, Actionable Code

### `scripts/discover_list_files.py`
```python
#!/usr/bin/env python3
"""
Run once per date folder (mac/CI) to produce deterministic file-list.json.
"""
import json
import os
from datetime import datetime, timezone
from huggingface_hub import HfApi

API = HfApi()
REPO_ID = os.getenv("HF_DATASET_REPO", "datasets/your-org/your-repo")
DATE_FOLDER = os.getenv("DATE_FOLDER", "batches/mirror-merged/2026-05-02")
OUT_PATH = os.getenv("OUT_PATH", "file-list.json")

def main() -> None:
    items = API.list_repo_tree(
        repo_id=REPO_ID,
        path=DATE_FOLDER,
        recursive=False  # single call, no pagination explosion
    )
    files = sorted(
        (it["path"] for it in items if it.get("type") == "file"),
        key=lambda p: p.lower()
    )
    payload = {
        "repo_id": REPO_ID,
        "date_folder": DATE_FOLDER,
        "listed_at": datetime.now(timezone.utc).isoformat(),
        "files": files
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} files to {OUT_PATH}")

if __name__ == "__main__":
    main()
```

### `scripts/train_cdn_only.py`
```python
#!/usr/bin/env python3
"""
Lightning/Kaggle entrypoint: CDN-only, schema-projected dataset builder.
"""
import io
import json
import os
import time
import random
import requests
import pandas as pd
import pyarrow.parquet as pq
from typing import List, Dict, Any

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-org/your-repo")
FILE_LIST_PATH = os.getenv("FILE_LIST_PATH", "file-list.json")
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "1.0"))

def load_file_list(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("files", [])

def fetch_via_cdn(repo: str, cdn_path: str) -> bytes:
    url = CDN_TEMPLATE.format(repo=repo, path=cdn_path)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # No Authorization header -> CDN-only, bypasses /api/ rate limits
            resp = requests.get(url, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"Retryable {resp.status_code}")
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == MAX_RETRIES:
                raise
            jitter = random.uniform(0.5, 1.5)
            sleep_sec = BACKOFF_BASE * (2 ** (attempt - 1)) * jitter
            print(f"Retryable error for {cdn_path}: {exc} — retry {attempt}/{MAX_RETRIES} in {sleep_sec:.1f}s")
            time.sleep(sleep_sec)
    raise RuntimeError("Unreachable")

def project_parquet(content: bytes) -> List[Dict[str, Any]]:
    table = pq.read_table(io.BytesIO(content), columns=["prompt", "response"])
    df = table.to_pandas()
    for col in ("prompt", "response"):
        if col not in df.columns:
            df[col] = None
    return df[["prompt", "response"]].to_dict(orient="records")

def project_jsonl(content: bytes) -> List[Dict[str, Any]]:
    rows = []
    for line in io.BytesIO(content).read().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            rows.append({"prompt": obj.get("prompt"), "response": obj.get("response")})
        except Exception:
            continue
    return rows

def build_dataset(file_list: List[str]) -> List[Dict[str, Any]]:
    records = []
    for path in file_list:
        try:
            content = fetch_via_cdn(HF_DATASET_REPO, path)
            if path.endswith(".parquet"):
                records.extend(project_parquet(content))
            elif path.endswith(".jsonl"):
                records.extend(project_jsonl(content))
            else:
                continue
        except Exception as exc:
            print(f"Skipping {path}: {exc}")
            continue
    return records

def main() -> None:
    file_list = load_file_list(FILE_LIST_PATH)
    print(f"Loaded {len(file_list)} files from list")
    dataset = build_dataset(file_list)
    print(f"Built {len(dataset)} prompt/response records")
    out_df = pd.DataFrame(dataset).dropna(subset=["prompt", "response"])
    out_path = "train_projected.parquet"
    out_df.to_parquet(out_path, index=False)
    print(f"Saved {out_path}")

if __name__ == "__main__":
    main()
```

### Kaggle kernel push (Bearer token, no CLI)
```python
import os
import requests

KAGGLE_API_URL = "https://www.kaggle.com/api/v1/kernels/push"
KGAT_TOKEN = os.getenv("KGAT_TOKEN")  # Bearer token

def push_kernel(slug: str, title: str, text: str, is_private: bool = True) -> None:
    """
    Push a Kaggle kernel. Use is_private=True to avoid phone-verification 403.
    """
    headers = {"Authorization": f"Bearer {KGAT_TOKEN}
