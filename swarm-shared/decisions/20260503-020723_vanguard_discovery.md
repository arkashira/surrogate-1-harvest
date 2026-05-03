# vanguard / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

**Diagnosis (merged, de-duplicated)**
- No persisted `(repo, dateFolder) → file-list` manifest: every run triggers authenticated `list_repo_tree`, burning HF API quota and risking 429s.
- Data loader likely uses `load_dataset(streaming=True)` or repeated per-file loads on heterogeneous repos, causing `pyarrow.CastError` schema failures.
- No CDN-only fetch path: authenticated API calls during ingestion/training are rate-limited; public CDN bypass is unused.
- Missing surrogate-1 schema hygiene: raw files with mixed schemas are pushed to `enriched/` instead of projecting to `{prompt, response}` and using `batches/mirror-merged/{date}/{slug}.parquet`.
- No reuse guard for Lightning Studio: training loops likely recreate studios instead of reusing running ones, wasting quota.

**Proposed change (merged)**
Create `/opt/axentx/vanguard/discovery/manifest.py` + `/opt/axentx/vanguard/discovery/train_manifest.py` (or patch existing orchestrator) to:
- Single-shot `list_repo_tree` (non-recursive, one date folder) from Mac after rate-limit window.
- Persist `manifest.json` with CDN paths.
- Embed manifest in training script so Lightning workers fetch via CDN only (zero API calls).
- Add schema projection and surrogate-1 filename convention for any local writes.
- Reuse running Lightning Studio when available; otherwise start L40S (free tier fallback).

Scope: add two small files and a one-line import/use in the main training launcher (or create if absent).

---

## Implementation (single, correct, actionable)

```bash
# /opt/axentx/vanguard/discovery/manifest.py
#!/usr/bin/env python3
"""
Generate and persist (repo, dateFolder) -> CDN file-list manifest.
Run once per dateFolder from Mac (or orchestrator) after HF API window clears.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    list_repo_tree = None

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

def build_manifest(repo: str, date_folder: str, out_path: str | None = None) -> dict:
    """
    Non-recursive tree listing for one dateFolder.
    Returns manifest and writes JSON.
    """
    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub required for manifest generation")

    # Single non-recursive call (paginated but small when scoped to dateFolder)
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    entries = [item.rfilename for item in tree if not item.rfilename.endswith("/")]

    # Build CDN paths (bypass API/auth)
    cdn_prefix = f"https://huggingface.co/datasets/{repo}/resolve/main"
    files = [
        {
            "repo": repo,
            "path": e,
            "cdn_url": f"{cdn_prefix}/{e}",
            "date_folder": date_folder,
        }
        for e in entries
    ]

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(files),
        "files": files,
    }

    if out_path is None:
        slug = repo.replace("/", "_")
        out_path = MANIFEST_DIR / f"{slug}__{date_folder}.json"
    else:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest

if __name__ == "__main__":
    # Example usage (run from Mac after rate-limit window):
    # python manifest.py <repo> <date_folder> [out.json]
    repo = sys.argv[1] if len(sys.argv) > 1 else "databricks/databricks-dolly-15k"
    date_folder = sys.argv[2] if len(sys.argv) > 2 else "2024-01-15"
    out = sys.argv[3] if len(sys.argv) > 3 else None
    m = build_manifest(repo, date_folder, out)
    print(f"Wrote manifest: {m['count']} files -> {out or 'default'}")
```

```python
# /opt/axentx/vanguard/discovery/train_manifest.py
#!/usr/bin/env python3
"""
Lightning-compatible dataset loader that uses CDN-only fetches via a prebuilt manifest.
No authenticated HF API calls during training.
"""
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

MANIFEST_DIR = Path(__file__).parent / "manifests"

class CDNParquetDataset(IterableDataset):
    """
    Stream rows from parquet files listed in a manifest using CDN URLs.
    Projects to {prompt, response} at parse time (surrogate-1 schema hygiene).
    """
    def __init__(self, manifest_path: str | Path, columns: Optional[List[str]] = None):
        super().__init__()
        self.manifest_path = Path(manifest_path)
        self.columns = columns or ["prompt", "response"]
        with open(self.manifest_path) as f:
            self.manifest = json.load(f)
        self.file_urls = [
            f["cdn_url"] for f in self.manifest["files"]
            if f["cdn_url"].endswith(".parquet")
        ]

    def _stream_parquet(self, url: str):
        # CDN fetch (no auth)
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        table = pq.read_table(pa.BufferReader(resp.content))

        # Project to surrogate-1 schema; coerce missing cols to null
        for col in self.columns:
            if col not in table.column_names:
                table = table.append_column(col, pa.array([None] * len(table)))
        table = table.select(self.columns)

        for batch in table.to_batches(max_chunksize=1024):
            for row in zip(*[batch.column(i).to_pylist() for i in range(len(self.columns))]):
                yield dict(zip(self.columns, row))

    def __iter__(self):
        for url in self.file_urls:
            yield from self._stream_parquet(url)

# Optional: Lightning launcher snippet (reuse running studio)
def launch_training_with_manifest(
    manifest_path: str,
    script_path: str,
    studio_name: str = "vanguard-train",
    env_overrides: Optional[Dict[str, str]] = None,
):
    """
    Reuse running Lightning Studio if available; else start L40S (free tier fallback).
    """
    try:
        from lightning import Studio, Machine, Teamspace
    except ImportError:
        print("lightning not installed; skipping studio reuse")
        return

    teamspace = Teamspace()
    studio = None
    for s in teamspace.studios:
        if s.name == studio_name and s.status == "Running":
            studio = s
            break

    if studio is None:
        # Free tier -> L40S; paid can target Machine.LightningLambdasH200 in lightning-lambda-prod
        studio = Studio(
            name=studio_name,
            machine=Machine.L40S,
            create_ok=True,
        )

    cmd = [
        "python",
        script_path,
        "--manifest",
        manifest_path,
    ]
    studio.run(cmd, wait=False, env=env_overrides)
    print(f"Launched on studio: {studio.name}")

if __name__ == "__main__":
    # CLI helper: python train_manifest.py <manifest.json>
    mp = sys.argv[1] if len(sys.argv) > 1 else (MANIFEST_DIR / "dolly_2024-01-15.json")
    ds = CDNParquetDataset(mp)
    for i, row in enumerate(ds):
        if i >= 3:
            break
        print(row)
```

```bash
# /opt/axentx/vanguard/discovery/run_discovery.sh
#!/usr/bin/env bash

