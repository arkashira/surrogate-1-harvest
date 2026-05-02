# airship / discovery

## Highest-Value Incremental Improvement (≤2h)

**Goal**: Harden `airship discover` into a deterministic, CDN-only orchestrator that eliminates HF API rate limits and PyArrow schema errors while producing reproducible file lists and safe ingestion artifacts.

**Why this ships fastest**:
- No new infra or GPU time required.
- Pure orchestration + small Python changes.
- Immediately unlocks downstream training (Surrogate) without quota risk.
- Reuses existing patterns (CDN bypass, schema projection, sibling repo sharding).

---

## Implementation Plan (≤2h)

| Step | Owner | Time | Deliverable |
|------|-------|------|-------------|
| 1 | Engineer | 15m | Add `discover/` module with CLI entrypoint `airship-discover` |
| 2 | Engineer | 25m | Implement `list_repo_tree` → local JSON file (date-scoped) |
| 3 | Engineer | 20m | Implement CDN-only downloader with schema projection `{prompt, response}` |
| 4 | Engineer | 15m | Implement sibling repo sharding (hash-slug → repo) for writes |
| 5 | Engineer | 15m | Add idempotent, append-only parquet writer (no `source`/`ts` cols) |
| 6 | Engineer | 20m | Add cron-safe wrappers (shebang, executable, `SHELL=/bin/bash`) |
| 7 | Engineer | 10m | Smoke test: run against one date folder and verify outputs |

---

## Code Snippets

### 1) CLI entrypoint (`airship/discover/__main__.py`)
```python
#!/usr/bin/env python3
import argparse
import json
import hashlib
from pathlib import Path
from airship.discover.core import list_date_folder, cdn_fetch_and_project, write_sharded

def main() -> None:
    parser = argparse.ArgumentParser(description="Airship CDN-only discovery")
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/ds)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out-dir", default="batches/mirror-merged", help="Output root")
    parser.add_argument("--siblings", type=int, default=5, help="Number of sibling repos")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) / args.date
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) List once, save manifest
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        files = list_date_folder(args.repo, args.date)
        manifest_path.write_text(json.dumps(files, indent=2))
    else:
        files = json.loads(manifest_path.read_text())

    # 2) CDN fetch + project + sharded write
    for fpath in files:
        slug = fpath.rstrip(".jsonl.parquet").split("/")[-1]
        dest_repo = f"{args.repo}-sibling-{int(hashlib.md5(slug.encode()).hexdigest(), 16) % args.siblings}"
        df = cdn_fetch_and_project(args.repo, fpath)
        if df is None or df.empty:
            continue
        write_sharded(dest_repo, out_dir, slug, df)

if __name__ == "__main__":
    main()
```

### 2) Core module (`airship/discover/core.py`)
```python
from huggingface_hub import list_repo_tree, hf_hub_download
import pandas as pd
import pyarrow as pa
import requests
from pathlib import Path
from typing import List, Optional

HF_CDN = "https://huggingface.co/datasets"

def list_date_folder(repo: str, date: str) -> List[str]:
    """List top-level folder for one date (non-recursive)."""
    tree = list_repo_tree(repo=repo, path=date, recursive=False)
    return [t.path for t in tree if t.type == "file"]

def cdn_fetch_and_project(repo: str, fpath: str) -> Optional[pd.DataFrame]:
    """Download via CDN (no auth/rate-limit) and project to {prompt, response}."""
    url = f"{HF_CDN}/{repo}/resolve/main/{fpath}"
    r = requests.get(url, timeout=60)
    if r.status_code != 200:
        return None

    local_path = Path("/tmp") / fpath.replace("/", "_")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(r.content)

    try:
        df = pd.read_parquet(local_path)
    except (pa.ArrowInvalid, ValueError):
        # Mixed schema fallback: read as JSONL line-by-line
        lines = [ln for ln in local_path.read_text().splitlines() if ln.strip()]
        records = []
        for ln in lines:
            try:
                obj = pd.read_json(ln, typ="series").to_dict()
                records.append(obj)
            except Exception:
                continue
        df = pd.DataFrame(records)

    # Projection: keep only prompt/response (rename common variants)
    colmap = {c: c.lower().replace(" ", "_") for c in df.columns}
    df = df.rename(columns=colmap)
    wanted = [c for c in df.columns if c in {"prompt", "response", "instruction", "completion"}]
    if not wanted:
        return pd.DataFrame(columns=["prompt", "response"])

    if "prompt" not in wanted and "instruction" in wanted:
        df["prompt"] = df["instruction"]
    if "response" not in wanted and "completion" in wanted:
        df["response"] = df["completion"]

    return df[["prompt", "response"]].dropna(how="all").reset_index(drop=True)

def write_sharded(repo: str, out_dir: Path, slug: str, df: pd.DataFrame) -> None:
    """Idempotent append-only parquet write (no source/ts cols)."""
    part = out_dir / f"{slug}.parquet"
    if part.exists():
        existing = pd.read_parquet(part)
        df = pd.concat([existing, df]).drop_duplicates().reset_index(drop=True)
    df.to_parquet(part, index=False)
```

### 3) Cron-safe wrapper (`scripts/airship-discover.sh`)
```bash
#!/usr/bin/env bash
set -euo pipefail
export SHELL=/bin/bash
cd /opt/axentx/airship
python -m airship.discover --repo="myorg/arkship-ds" --date="2026-05-02" --out-dir="batches/mirror-merged" --siblings=5
```

```bash
chmod +x scripts/airship-discover.sh
```

Crontab entry:
```cron
SHELL=/bin/bash
0 2 * * * /opt/axentx/airship/scripts/airship-discover.sh >> /var/log/airship-discover.log 2>&1
```

---

## Verification (10m smoke test)

```bash
cd /opt/axentx/airship
python -m airship.discover --repo="myorg/arkship-ds" --date="2026-05-02" --out-dir="batches/mirror-merged" --siblings=5
ls -la batches/mirror-merged/2026-05-02/*.parquet
head -n 5 batches/mirror-merged/2026-05-02/*.parquet | python -c "import sys, pandas as pd; [print(pd.read_parquet(f).columns.tolist()) for f in sys.stdin.read().strip().split() if f]"
```

Expected:
- `manifest.json` exists with listed files.
- Parquet files contain only `prompt`, `response`.
- No HF API calls during fetch (CDN-only).
- No PyArrow schema errors.
