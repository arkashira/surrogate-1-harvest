# vanguard / backend

## Final Synthesized Implementation (single, actionable)

**Core fixes**
- Persist a **non-recursive, date-folder-scoped manifest** keyed by `(repo, dateFolder)` to eliminate repeated HF API enumeration and 429 risk.
- Use **CDN-only URLs** for all data fetches (zero authenticated API calls during training).
- Avoid `load_dataset(streaming=True)` on heterogeneous repos; read parquet via CDN and project only required columns to prevent pyarrow `CastError`.
- Reuse running Lightning Studio and guard against idle-stop/creation loops to protect quota.

---

### 1) Manifest module
`/opt/axentx/vanguard/backend/manifest.py`
```python
#!/usr/bin/env python3
"""
Persisted (repo, dateFolder) manifest with CDN-only URLs.
- Single non-recursive HF API call per (repo, dateFolder) when stale (>24h).
- All subsequent training loads use CDN URLs (no auth, no API quota).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from huggingface_hub import HfApi, RepoTreeItem


MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)


def _manifest_path(repo_id: str, date_folder: str) -> Path:
    safe_repo = repo_id.replace("/", "__")
    return MANIFEST_DIR / f"{safe_repo}__{date_folder}.json"


def _cdn_url(repo_id: str, date_folder: str, filename: str) -> str:
    return (
        f"https://huggingface.co/datasets/{repo_id}/resolve/main/"
        f"{date_folder}/{filename}"
    )


def build_manifest(
    repo_id: str,
    date_folder: str,
    api: Optional[HfApi] = None,
    *,
    ttl_seconds: int = 86400,
) -> Dict:
    """
    Build or reuse a manifest for one date folder (non-recursive).
    Returns dict with repo_id, date_folder, generated_at_utc, files[].
    Each file: filename, cdn_url, size.
    """
    api = api or HfApi()
    manifest_file = _manifest_path(repo_id, date_folder)

    # Reuse fresh manifest
    if manifest_file.exists():
        mtime = datetime.fromtimestamp(manifest_file.stat().st_mtime, tz=timezone.utc)
        if (datetime.now(tz=timezone.utc) - mtime).total_seconds() < ttl_seconds:
            return json.loads(manifest_file.read_text())

    # Single non-recursive call
    items: List[RepoTreeItem] = list(
        api.list_repo_tree(
            repo_id=repo_id,
            path=date_folder,
            recursive=False,
            repo_type="dataset",
        )
    )

    files = []
    for item in items:
        if item.type != "file" or not item.path:
            continue
        filename = item.path.split("/")[-1]
        files.append(
            {
                "filename": filename,
                "cdn_url": _cdn_url(repo_id, date_folder, filename),
                "size": getattr(item, "size", None),
            }
        )

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "files": files,
    }
    manifest_file.write_text(json.dumps(manifest, indent=2))
    return manifest


def load_manifest(repo_id: str, date_folder: str) -> Dict:
    p = _manifest_path(repo_id, date_folder)
    if not p.exists():
        raise FileNotFoundError(f"Manifest not found: {p}")
    return json.loads(p.read_text())


def cdn_file_urls(manifest: Dict) -> List[str]:
    return [f["cdn_url"] for f in manifest.get("files", [])]
```

---

### 2) Data loader (CDN + schema-safe)
`/opt/axentx/vanguard/backend/train_loader.py`
```python
from __future__ import annotations

import pyarrow.parquet as pq
import requests
from io import BytesIO
from typing import List, Tuple


def cdn_parquet_to_pairs(
    urls: List[str],
    *,
    prompt_col: str = "prompt",
    response_col: str = "response",
    fallback_prompt_col: str = "input",
    fallback_response_col: str = "output",
) -> List[Tuple[str, str]]:
    """
    Fetch parquet files via CDN and extract (prompt, response) pairs.
    Projects only required columns to avoid mixed-schema CastError.
    """
    pairs = []
    for url in urls:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        table = pq.read_table(BytesIO(resp.content))

        pc = prompt_col if prompt_col in table.column_names else fallback_prompt_col
        rc = response_col if response_col in table.column_names else fallback_response_col

        if pc not in table.column_names or rc not in table.column_names:
            continue

        prompts = table.column(pc).to_pylist()
        responses = table.column(rc).to_pylist()
        for p, r in zip(prompts, responses):
            if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                pairs.append((p.strip(), r.strip()))
    return pairs
```

---

### 3) Training orchestration with Studio reuse
`/opt/axentx/vanguard/backend/train.py` (key patch)
```python
import lightning as L
from pathlib import Path

from vanguard.backend.manifest import build_manifest, cdn_file_urls
from vanguard.backend.train_loader import cdn_parquet_to_pairs


def prepare_and_run(
    repo_id: str,
    date_folder: str,
    script_path: str,
    *,
    studio_name: Optional[str] = None,
    machine=L.Machine.L40S,
):
    # 1) Manifest + CDN URLs (single API call when stale)
    manifest = build_manifest(repo_id, date_folder)
    urls = cdn_file_urls(manifest)
    print(f"Prepared {len(urls)} files via CDN for {repo_id}/{date_folder}")

    # 2) Optional: materialize pairs once and cache for training script
    # pairs = cdn_parquet_to_pairs(urls)
    # manifest["pairs_path"] = ... (save to disk if needed)

    # 3) Reuse running Studio; avoid repeated creation
    teamspace = L.Teamspace()
    studio_name = studio_name or f"vanguard-{date_folder}"
    studio = next(
        (s for s in teamspace.studios if s.name == studio_name and s.status == "running"),
        None,
    )

    if studio is None:
        print(f"Creating studio: {studio_name}")
        studio = teamspace.create_studio(name=studio_name, machine=machine)

    if studio.status != "running":
        print("Starting studio...")
        studio.start(machine=machine)

    # 4) Launch training with CDN-driven inputs
    job = studio.run(
        target=script_path,
        arguments=[
            "--manifest-repo", repo_id,
            "--manifest-date", date_folder,
            "--use-cdn",
        ],
    )
    print(f"Launched job via studio {studio_name}")
    return job, studio
```

---

### 4) Verification (concrete commands)
```bash
# 1) Build manifest and confirm file count
python3 -c "
from vanguard.backend.manifest import build_manifest
m = build_manifest('datasets/company-docs', '2026-05-01')
print('files:', len(m['files']))
"

# 2) Confirm first CDN URL is reachable without auth
python3 -c "
from vanguard.backend.manifest import build_manifest
m = build_manifest('datasets/company-docs', '2026-05-01')
import subprocess, sys
url = m['files'][0]['cdn_url']
subprocess.run([sys.executable, '-c', f'import urllib.request; r=urllib.request.urlopen(urllib.request.Request(\"{url}\
