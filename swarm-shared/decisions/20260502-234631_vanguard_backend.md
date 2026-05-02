# vanguard / backend

## Final Synthesis (Best Parts + Correctness + Actionability)

Below is the **single, authoritative implementation plan** that merges the strongest insights from both candidates, removes contradictions, and prioritizes correctness and concrete actionability.

---

## 1. Diagnosis (Consensus)
- **No persisted manifest** → every training job re-calls HF listing APIs → 429 risk.
- **Training uses HF auth flows** (`load_dataset`, `hf_hub_download`) instead of CDN → lower effective rate limits and auth overhead.
- **Lightning Studio recreate-on-run** → quota burn and cold-start latency.
- **No deterministic HF write sharding** → ingestion can hit 128 commits/hr/repo cap.
- **Missing lightweight manifest generation** in orchestration → frontend and backend both re-query HF.

---

## 2. Proposed Change (Unified)
Add three focused backend modules and update orchestration:

- `/opt/axentx/vanguard/backend/manifest.py`  
  Single-call, date-scoped manifest generator with CDN URLs.

- `/opt/axentx/vanguard/backend/train_loader.py`  
  CDN-only parquet streamer (no HF auth) for training.

- `/opt/axentx/vanguard/backend/shard_router.py`  
  Deterministic slug → sibling repo router to spread HF commit load.

- Update `/opt/axentx/vanguard/backend/orchestrator.py`  
  1) Generate/reuse manifest once per date folder.  
  2) Pass manifest path to training.  
  3) Reuse running Lightning Studio (start if stopped).  
  4) Use shard router for any HF writes.

---

## 3. Implementation

### manifest.py
```python
# /opt/axentx/vanguard/backend/manifest.py
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import list_repo_tree
except ImportError as e:
    raise RuntimeError("huggingface_hub required for manifest generation") from e


def build_manifest(
    repo_id: str,
    date_folder: str,
    out_path: str,
    revision: str = "main",
    recursive: bool = False,
) -> List[Dict[str, str]]:
    """
    Single API call to list files in one date folder and persist manifest.
    Manifest format:
      {
        "repo_id": "...",
        "date_folder": "...",
        "revision": "...",
        "generated_at": "...",
        "files": [
          {"path": "...", "cdn_url": "...", "size": ...},
          ...
        ]
      }
    """
    items = list_repo_tree(
        repo_id=repo_id,
        path=date_folder,
        revision=revision,
        recursive=recursive,
    )

    files = []
    for item in items:
        if item.rfilename.endswith(".parquet"):
            cdn_url = (
                f"https://huggingface.co/datasets/{repo_id}/resolve/main/{item.rfilename}"
            )
            files.append(
                {
                    "path": item.rfilename,
                    "cdn_url": cdn_url,
                    "size": getattr(item, "size", None),
                }
            )

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "revision": revision,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    out.write_text(json.dumps(manifest, indent=2))
    return manifest["files"]


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python -m vanguard.backend.manifest <repo_id> <date_folder> [out_path]")
        sys.exit(1)

    repo_id = sys.argv[1]
    date_folder = sys.argv[2]
    out_path = sys.argv[3] if len(sys.argv) > 3 else "manifest.json"
    files = build_manifest(repo_id, date_folder, out_path)
    print(f"Wrote {len(files)} files to {out_path}")
```

### train_loader.py
```python
# /opt/axentx/vanguard/backend/train_loader.py
import json
from pathlib import Path
from typing import Iterator, Dict, Any, Tuple, Optional
import pyarrow.parquet as pq
import requests
from io import BytesIO


def cdn_parquet_stream(cdn_url: str, timeout: int = 60) -> pq.ParquetFile:
    """
    Download a single parquet via CDN (no auth/rate-limit) and return ParquetFile.
    """
    resp = requests.get(cdn_url, timeout=timeout)
    resp.raise_for_status()
    return pq.ParquetFile(BytesIO(resp.content))


def iter_cdn_records(
    manifest_path: str,
    columns: Optional[Tuple[str, ...]] = ("prompt", "response"),
    max_files: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    """
    Iterate records from manifest using CDN-only downloads.
    """
    manifest = json.loads(Path(manifest_path).read_text())
    files = manifest.get("files", [])
    if max_files is not None:
        files = files[:max_files]

    for entry in files:
        pf = cdn_parquet_stream(entry["cdn_url"])
        table = pf.read(columns=columns) if columns else pf.read()
        for row in table.to_pylist():
            yield row


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m vanguard.backend.train_loader <manifest_path> [max_files]")
        sys.exit(1)

    manifest = sys.argv[1]
    max_files = int(sys.argv[2]) if len(sys.argv) > 2 else None
    for i, rec in enumerate(iter_cdn_records(manifest, max_files=max_files)):
        if i >= 5:
            break
        print(rec)
```

### shard_router.py
```python
# /opt/axentx/vanguard/backend/shard_router.py
import hashlib
from typing import List


def pick_sibling_repo(slug: str, siblings: List[str]) -> str:
    """
    Deterministic shard-to-repo mapping to spread HF commit load.
    siblings = ["repo-a", "repo-b", "repo-c", "repo-d", "repo-e"]
    """
    if not siblings:
        raise ValueError("siblings list must not be empty")
    digest = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    idx = digest % len(siblings)
    return siblings[idx]


if __name__ == "__main__":
    siblings = [
        "vanguard-mirror-0",
        "vanguard-mirror-1",
        "vanguard-mirror-2",
        "vanguard-mirror-3",
        "vanguard-mirror-4",
    ]
    import sys

    slug = sys.argv[1] if len(sys.argv) > 1 else "example-slug-2026-05-02"
    print(pick_sibling_repo(slug, siblings))
```

### orchestrator.py (key updates)
```python
# /opt/axentx/vanguard/backend/orchestrator.py
import os
from pathlib import Path
from typing import Optional

from huggingface_hub import whoami
from lightning import Studio

from vanguard.backend.manifest import build_manifest
from vanguard.backend.shard_router import pick_sibling_repo


def ensure_studio(name: str, project: str = "vanguard") -> Studio:
    """
    Reuse a running Lightning Studio if available; start if stopped;
    create if missing. Avoids recreate-on-each-run.
    """
    # Lightning SDK usage pattern (pseudo-code - adapt to actual SDK):
    # studio = Studio(name=name, project=project)
    # if not studio.exists():
    #     studio.create()
    # elif studio.status == "STOPPED":
    #     studio.start()
    # studio.wait_until_running()
    # return studio
    raise NotImplementedError("Adapt to Lightning SDK for your environment")


def prepare_manifest(
    repo_id: str,
    date
