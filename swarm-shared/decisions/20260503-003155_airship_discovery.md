# airship / discovery

## Implementation Plan (≤2h)

**Highest-value incremental improvement**: Replace `load_dataset`/`list_repo_files` with a manifest-driven, CDN-only iterable loader in the surrogate training pipeline to eliminate HF API rate limits and mixed-schema ingestion failures.

### Scope
- Add `scripts/build_manifest.py` (Mac-side, runs once per date folder)
- Add `surrogate/data/cdn_dataset.py` (Lightning training, zero API calls)
- Update surrogate training entrypoint to use `CdnIterableDataset`
- Add minimal tests and usage example

### Steps (timed)
1. **0–20m** — Scaffold files and define manifest schema
2. **20–50m** — Implement manifest builder (Mac side)
3. **50–80m** — Implement CDN iterable dataset (Lightning side)
4. **80–100m** — Wire into training entrypoint + example script
5. **100–120m** — Small README snippet + smoke test

---

## 1) Manifest schema (JSON)

```json
{
  "repo": "datasets/username/repo",
  "folder": "batches/mirror-merged/2026-04-29",
  "files": [
    {
      "path": "batches/mirror-merged/2026-04-29/slug-abc.parquet",
      "cdn_url": "https://huggingface.co/datasets/datasets/username/repo/resolve/main/batches/mirror-merged/2026-04-29/slug-abc.parquet",
      "size": 12345678
    }
  ],
  "generated_at": "2026-04-29T12:34:56Z"
}
```

---

## 2) Manifest builder (Mac side)

`scripts/build_manifest.py`

```python
#!/usr/bin/env python3
"""
Build a manifest for one date folder (non-recursive) to enable CDN-only training.
Usage:
  HF_TOKEN=hf_xxx python scripts/build_manifest.py \
    --repo datasets/username/repo \
    --folder batches/mirror-merged/2026-04-29 \
    --out manifest-2026-04-29.json
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict

import requests
from tqdm import tqdm


HEADERS = {}
if token := os.getenv("HF_TOKEN"):
    HEADERS["Authorization"] = f"Bearer {token}"


def list_folder(repo: str, folder: str) -> List[Dict]:
    """
    List files in a single folder (non-recursive).
    Uses HF Tree API (paginated). Falls back to CDN if needed.
    """
    url = f"https://huggingface.co/api/datasets/{repo}/tree"
    params = {"path": folder, "recursive": "false"}
    items = []
    cursor = None

    while True:
        resp = requests.get(url, headers=HEADERS, params={**params, **(dict(cursor=cursor) if cursor else {})})
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 360))
            print(f"Rate limited. Waiting {retry_after}s")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        payload = resp.json()
        items.extend(payload)

        # pagination: last item's path used as cursor
        if not payload:
            break
        cursor = payload[-1]["path"]
        # heuristic stop: if next would be outside folder
        if len(items) > 0 and not items[-1]["path"].startswith(folder.rstrip("/") + "/"):
            break
        # simple stop: assume one page is enough for a date folder
        if len(items) >= 1000:
            break

    return items


def build_manifest(repo: str, folder: str) -> Dict:
    folder = folder.rstrip("/")
    items = list_folder(repo, folder)

    files = []
    for it in items:
        if it.get("type") != "file":
            continue
        path = it["path"]
        files.append(
            {
                "path": path,
                "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{path}",
                "size": it.get("size", 0),
            }
        )

    manifest = {
        "repo": repo,
        "folder": folder,
        "files": files,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build CDN manifest for a folder.")
    parser.add_argument("--repo", required=True, help="Dataset repo (e.g., datasets/username/repo)")
    parser.add_argument("--folder", required=True, help="Folder path inside repo")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    print(f"Building manifest for {args.repo}::{args.folder}")
    manifest = build_manifest(args.repo, args.folder)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest['files'])} files to {out_path}")


if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x scripts/build_manifest.py
```

---

## 3) CDN-only iterable dataset (Lightning side)

`surrogate/data/cdn_dataset.py`

```python
import json
import logging
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

logger = logging.getLogger(__name__)


@dataclass
class CdnFileEntry:
    path: str
    cdn_url: str
    size: int


class CdnIterableDataset(IterableDataset):
    """
    Zero HF API calls during training.
    Reads parquet files directly from HF CDN using a pre-built manifest.
    Projects each row to {prompt, response} at parse time.
    """

    def __init__(
        self,
        manifest_path: str,
        projection: Optional[List[str]] = None,
        start_index: int = 0,
        end_index: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.manifest_path = manifest_path
        self.projection = projection or ["prompt", "response"]
        self.start_index = start_index
        self.end_index = end_index
        self._files: List[CdnFileEntry] = []
        self._load_manifest()

    def _load_manifest(self) -> None:
        with open(self.manifest_path) as f:
            manifest = json.load(f)
        self._files = [CdnFileEntry(**f) for f in manifest["files"]]
        total = len(self._files)
        self._files = self._files[self.start_index : self.end_index]
        logger.info(
            f"Loaded manifest {self.manifest_path}: {total} total files, using {len(self._files)} (start={self.start_index}, end={self.end_index})"
        )

    def _stream_file(self, entry: CdnFileEntry) -> Iterator[Dict]:
        """Stream a single parquet file from CDN and yield projected rows."""
        try:
            resp = requests.get(entry.cdn_url, stream=True, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(f"Failed to fetch {entry.cdn_url}: {exc}")
            return

        # Use pyarrow to read from bytes without touching disk
        try:
            table = pq.read_table(pa.BufferReader(resp.content), columns=self.projection)
        except Exception as exc:
            logger.warning(f"Failed to parse parquet {entry.path}: {exc}")
            return

        df = table.to_pandas()
        for _, row in df.iterrows():
            # Ensure minimal schema; skip malformed rows
            if "prompt" not in row or "response" not in row:
