# airship / discovery

## Final Synthesized Implementation  
*(Best parts of Candidate 1 + Candidate 2, contradictions resolved for correctness + concrete actionability)*

---

## Core Design Decisions (Resolved Contradictions)

1. **Manifest scope**: generate **per-date folder** (not per-file) to minimize API calls, but include **recursive file listing** so no file is missed.  
   - *Why*: `list_repo_tree(recursive=False)` per date folder is one API call per date; recursive listing inside each date is free (local filtering).  
   - *Action*: generator script walks date folders, one API call per folder.

2. **CDN vs API during training**: **CDN-only during training**, but allow **fallback to `hf_api` for manifest generation only** (with retries/backoff).  
   - *Why*: training must be immune to 429s; manifest generation can tolerate retries and runs offline/cron.

3. **Schema heterogeneity**: **project to `{prompt, response}` at parse time** with field aliases and strict validation; log/reject malformed lines instead of crashing.  
   - *Why*: avoids `pyarrow.CastError` and keeps training stable.

4. **Streaming vs materialization**: use **`IterableDataset` with streaming** for memory efficiency, but provide **optional `Dataset.from_generator`** for small/debug runs.  
   - *Why*: Surrogate training may be large; streaming is safer default.

5. **Cron safety + rate limits**: **exponential backoff + jitter** on 429, **lockfile** to prevent concurrent runs, **wait 360s after 429** before retry.  
   - *Why*: cron can overlap; HF rate limits need deterministic cooldown.

6. **Lightning Studio reuse**: **explicit reuse of running studio by name**; create only if none running.  
   - *Why*: saves quota; deterministic behavior.

---

## File Tree (New/Modified)

```
airship/surrogate/
├── data/
│   ├── cdn_dataset.py          # streaming CDN loader + IterableDataset
│   └── manifest.py             # manifest types + loader
├── scripts/
│   ├── generate_manifest.py    # cron-safe manifest generator (per-date)
│   └── cron_generate.sh        # cron wrapper with lockfile + backoff
├── train_cdn.py                # training entrypoint (manifest-driven)
└── utils/
    └── retry.py                # exponential backoff + jitter
```

---

## Implementation

### 1. `airship/surrogate/utils/retry.py`
```python
import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")

def backoff_retry(
    fn: Callable[[], T],
    *,
    retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 360.0,
    on_429_delay: float = 360.0,
) -> T:
    """Exponential backoff with jitter. Special long delay on 429."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            is_429 = "429" in str(exc)
            delay = on_429_delay if is_429 else min(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1), max_delay)
            print(f"Attempt {attempt}/{retries} failed: {exc}. Waiting {delay:.1f}s")
            if attempt == retries:
                break
            time.sleep(delay)
    raise last_exc
```

### 2. `airship/surrogate/data/manifest.py`
```python
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

@dataclass
class FileEntry:
    repo: str
    path: str
    size: int
    sha: str
    cdn_url: str
    date: str

@dataclass
class Manifest:
    repo: str
    date: str
    generated_at: str
    files: List[FileEntry]
    total_files: int
    total_bytes: int

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["files"] = [asdict(f) for f in self.files]
        return d

    @staticmethod
    def from_dict(data: Dict) -> "Manifest":
        files = [FileEntry(**f) for f in data["files"]]
        return Manifest(
            repo=data["repo"],
            date=data["date"],
            generated_at=data["generated_at"],
            files=files,
            total_files=data["total_files"],
            total_bytes=data["total_bytes"],
        )

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @staticmethod
    def load(path: Path) -> "Manifest":
        with open(path) as f:
            return Manifest.from_dict(json.load(f))
```

### 3. `airship/surrogate/scripts/generate_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate CDN-only manifest for a HuggingFace dataset repo (per-date folder).
Usage: python generate_manifest.py --repo <org/repo> --date 2026-05-03 --out manifest-2026-05-03.json
"""
import argparse
import time
from pathlib import Path

from huggingface_hub import HfApi

from airship.surrogate.data.manifest import Manifest, FileEntry
from airship.surrogate.utils.retry import backoff_retry

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_folder_safe(api: HfApi, repo: str, date: str):
    """Single API call per date folder with backoff."""
    return backoff_retry(
        lambda: list(api.list_repo_tree(repo=repo, path=date, recursive=True)),
        on_429_delay=360.0,
        retries=5,
    )

def generate_manifest(repo: str, date: str, out_path: Path):
    api = HfApi()
    tree_items = list_date_folder_safe(api, repo, date)

    files = []
    for item in tree_items:
        if item.type != "file":
            continue
        # Only include files under the exact date prefix (safety)
        if not item.path.startswith(f"{date}/"):
            continue
        files.append(
            FileEntry(
                repo=repo,
                path=item.path,
                size=item.size,
                sha=getattr(item, "commit_id", None) or (item.lfs.get("oid") if hasattr(item, "lfs") else ""),
                cdn_url=CDN_TEMPLATE.format(repo=repo, path=item.path),
                date=date,
            )
        )

    manifest = Manifest(
        repo=repo,
        date=date,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        files=files,
        total_files=len(files),
        total_bytes=sum(f.size for f in files),
    )
    manifest.save(out_path)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate CDN manifest for HF dataset repo.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (org/repo)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default="manifest-latest.json", help="Output JSON path")
    args = parser.parse_args()
    generate_manifest(args.repo, args.date, Path(args.out))
```

### 4. `airship/surrogate/scripts/cron_generate.sh`
```bash
#!/usr/bin/env bash
# Cron-safe manifest generator with lockfile and long backoff on 429.
# SHELL=/bin/bash in crontab required.

set -euo pipefail

REPO="your-org/your-repo"
DATE="${1:-$(date -u +%Y-%m-%d)}"
OUT_DIR="/path
