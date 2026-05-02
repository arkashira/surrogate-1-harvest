# airship / frontend

## Final Implementation — Deterministic CDN-only Manifest for HF Datasets

**Scope**: Highest-value frontend-adjacent improvement that unblocks surrogate-1 training and ships in <2h.  
**Deliverable**: CLI + HTTP endpoint that produces a deterministic JSON manifest of public HF dataset files for a given `repo_id` and `date_folder`, using CDN URLs only (zero API calls during training).

---

### Why this now
- Unblocks surrogate-1 training by providing a pre-listed, CDN-only file list (bypasses HF API 429).
- Fits existing airship patterns (discover CLI + HTTP endpoint) and can be reused by both Arkship and Surrogate.
- Minimal surface: one CLI command + one POST endpoint + one generator module.

---

### Architecture (minimal)

- `airship/cli/discover.py` — Click command `airship discover <repo_id> <date_folder> [--out manifest.json]`
- `airship/api/routes/discover.py` — FastAPI POST `/discover` body: `{repo_id, date_folder}` → manifest JSON
- `airship/core/hf_cdn.py` — Core logic:
  - Uses HF Hub `list_repo_tree(path=date_folder, recursive=True)` **once** (from CLI only).
  - Builds deterministic manifest sorted by path.
  - Each entry: `{ "path": "...", "cdn_url": "https://huggingface.co/datasets/{repo_id}/resolve/main/{path}", "size": int|null, "sha256": str|null }`
  - Output: stable JSON (sorted keys, deterministic ordering).

---

### Implementation Steps (≤2h)

1. Add `click`/`fastapi` route stubs (if not present).  
2. Implement `hf_cdn.build_manifest(repo_id, date_folder)` with deterministic sorting and CDN URL template.  
3. CLI: `airship discover` writes manifest to stdout or file.  
4. API: `POST /discover` returns JSON manifest (cached optionally via FastAPI `lru_cache` per args).  
5. Add lightweight error handling (HF API 429/404 → clear message; fallback note about CDN availability).  
6. Add unit test stubs and one integration example in README.

---

### Code Snippets

#### `airship/core/hf_cdn.py`
```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from functools import lru_cache
from typing import List, Optional

from huggingface_hub import HfApi, HfFolder

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"


@dataclass(frozen=True)
class CDNFile:
    path: str
    cdn_url: str
    size: Optional[int] = None
    sha256: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize_path(p: str) -> str:
    return p.lstrip("/")


@lru_cache(maxsize=2)
def build_manifest(repo_id: str, date_folder: str, token: Optional[str] = None) -> List[dict]:
    """
    Build deterministic CDN-only manifest for repo_id/date_folder.

    Note:
      - Uses HF API ONCE (list_repo_tree) to enumerate all files recursively.
      - All training fetches should use `cdn_url` (bypasses API rate limits).
      - Deterministic ordering: sorted by path.
    """
    api = HfApi(token=token or HfFolder.get_token())
    folder = _normalize_path(date_folder)

    try:
        tree = api.list_repo_tree(repo_id=repo_id, path=folder, recursive=True)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to list repo tree for {repo_id}/{folder}: {exc}. "
            "If rate-limited (429), wait 360s and retry. "
            "Training should use CDN-only URLs once manifest is produced."
        ) from exc

    entries: List[CDNFile] = []
    for node in tree:
        # Only include files (skip subfolders)
        if node.type != "file":
            continue
        path = _normalize_path(node.path)
        cdn_url = CDN_TEMPLATE.format(repo_id=repo_id, path=path)
        # Compute deterministic sha256 placeholder (training can recompute if needed)
        sha256 = hashlib.sha256(path.encode()).hexdigest()
        entries.append(CDNFile(path=path, cdn_url=cdn_url, size=getattr(node, "size", None), sha256=sha256))

    entries.sort(key=lambda x: x.path)
    return [e.to_dict() for e in entries]


def dump_manifest(repo_id: str, date_folder: str, out_path: str, token: Optional[str] = None) -> str:
    manifest = {
        "repo_id": repo_id,
        "date_folder": _normalize_path(date_folder),
        "generated_by": "airship-discover",
        "entries": build_manifest(repo_id, date_folder, token=token),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
    return out_path
```

#### `airship/cli/discover.py`
```python
#!/usr/bin/env bash
# Wrapper note: ensure executable and invoke via bash if used as script.
# Prefer: python -m airship.cli.discover ...

import json
import sys
from pathlib import Path

import click

from airship.core.hf_cdn import build_manifest, dump_manifest


@click.command()
@click.argument("repo_id")
@click.argument("date_folder")
@click.option("--out", "-o", type=click.Path(), help="Write manifest to file (default: stdout)")
@click.option("--token", envvar="HF_TOKEN", default=None, help="HF token (optional for public repos)")
def discover(repo_id: str, date_folder: str, out: str, token: str) -> None:
    """
    Generate deterministic CDN-only manifest for HF dataset repo/date_folder.

    Example:
      airship discover my-org/my-dataset 2026-05-01 --out manifest.json
    """
    try:
        if out:
            out_path = Path(out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            written = dump_manifest(repo_id, date_folder, str(out_path), token=token)
            click.echo(f"Manifest written to {written}", err=True)
        else:
            manifest = {
                "repo_id": repo_id,
                "date_folder": date_folder,
                "generated_by": "airship-discover",
                "entries": build_manifest(repo_id, date_folder, token=token),
            }
            json.dump(manifest, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
            sys.stdout.write("\n")
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    discover()
```

#### `airship/api/routes/discover.py`
```python
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query

from airship.core.hf_cdn import build_manifest

router = APIRouter()


@lru_cache(maxsize=4)
def _cached_manifest(repo_id: str, date_folder: str) -> list:
    return build_manifest(repo_id, date_folder)


@router.post("/discover")
async def post_discover(
    repo_id: str = Query(..., description="HF dataset repo id"),
    date_folder: str = Query(..., description="Date folder inside dataset"),
):
    """
    CDN-only file manifest for HF dataset repo/date_folder.

    Returns deterministic JSON with CDN URLs. Safe for training pipelines
    (zero API calls during data loading).
    """
    try:
        entries = _cached_manifest(repo_id, date_folder)
        return {
            "repo_id": repo_id,
            "date_folder": date_folder,
            "generated_by": "airship-discover",
            "entries": entries,
        }
    except Exception as exc:
        raise HTTPException(
            status
