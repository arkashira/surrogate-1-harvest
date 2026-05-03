# vanguard / backend

## Final Synthesis (correctness + concrete actionability)

**Core problem**: training and UI depend on live HF API calls and mutable dataset layouts, causing 429s, non-reproducible epochs, and silent schema drift.  
**Required outcome**: a single, content-addressed snapshot that training and UI can pin, with deterministic schema projection and integrity verification.

---

## 1. Canonical manifest (content-addressed, CDN-first)

Create `/opt/axentx/vanguard/backend/manifest.py` (single source of truth) and CLI `/opt/axentx/vanguard/backend/build_manifest.py`.

```python
# /opt/axentx/vanguard/backend/manifest.py
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests

try:
    from huggingface_hub import hf_hub_download, list_repo_tree
except ImportError as e:
    raise RuntimeError("huggingface_hub required") from e


@dataclass(frozen=True)
class FileEntry:
    path: str
    sha256: str
    size: int
    cdn_url: str

    @classmethod
    def from_content(cls, repo: str, path: str, content: bytes) -> "FileEntry":
        return cls(
            path=path,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            cdn_url=f"https://huggingface.co/datasets/{repo}/resolve/main/{path}",
        )


@dataclass
class Manifest:
    repo: str
    folder: str
    generated_at: str
    generator: str = "vanguard-manifest/1.0"
    files: List[FileEntry]

    def __post_init__(self) -> None:
        # keep serializable shape
        object.__setattr__(self, "files", list(self.files))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["files"] = [asdict(f) for f in self.files]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        d = dict(d)
        d["files"] = [FileEntry(**f) for f in d.pop("files", [])]
        d.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
        return cls(**d)

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def verify(self, max_workers: int = 8, timeout: int = 30) -> List[FileEntry]:
        bad: List[FileEntry] = []

        def _check(e: FileEntry) -> Optional[FileEntry]:
            try:
                r = requests.get(e.cdn_url, timeout=timeout, stream=True)
                r.raise_for_status()
                h = hashlib.sha256()
                for chunk in r.iter_content(chunk_size=1 << 20):
                    h.update(chunk)
                if h.hexdigest() != e.sha256:
                    return e
            except Exception:
                return e
            return None

        with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for result in ex.map(_check, self.files):
                if result:
                    bad.append(result)
        return bad


def build_manifest(repo: str, folder: str, out_path: Path) -> Manifest:
    """
    Deterministic manifest for one folder.
    One list_repo_tree call + CDN downloads for hashes.
    """
    items = list_repo_tree(repo=repo, path=folder, recursive=False)
    file_paths = sorted(it.rfilename for it in items if it.type == "file")

    entries: List[FileEntry] = []
    for fp in file_paths:
        local = hf_hub_download(repo_id=repo, filename=fp, repo_type="dataset")
        content = Path(local).read_bytes()
        entries.append(FileEntry.from_content(repo, fp, content))

    manifest = Manifest(
        repo=repo,
        folder=folder,
        generated_at=datetime.now(timezone.utc).isoformat(),
        files=entries,
    )
    manifest.save(out_path)
    return manifest


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    build_manifest(args.repo, args.folder, Path(args.out))
    print(f"Manifest written to {args.out}")
```

---

## 2. Deterministic schema projection (training-ready)

Create `/opt/axentx/vanguard/backend/project_parquet.py` to convert enriched parquet into canonical `{prompt,response}` shards with content addressing.

```python
# /opt/axentx/vanguard/backend/project_parquet.py
from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
from pathlib import Path


REQUIRED = {"prompt", "response"}
ALLOWED_EXTRA = {"source", "ts"}  # accepted but stripped on output


def project_parquet(in_path: Path, out_path: Path) -> dict:
    """Project parquet to {prompt,response} and return content hash metadata."""
    tbl = pq.read_table(in_path)
    cols = set(tbl.column_names)

    missing = REQUIRED - cols
    if missing:
        raise ValueError(f"Missing columns {missing} in {in_path}")

    # keep only required columns; drop extras for canonical training data
    canonical = tbl.select(list(REQUIRED))

    pq.write_table(canonical, out_path)

    sha = _sha256_file(out_path)
    return {"source_file": str(in_path), "sha256": sha, "rows": canonical.num_rows}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    import argparse, json, hashlib

    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--out", dest="out_path", required=True)
    args = ap.parse_args()
    meta = project_parquet(Path(args.in_path), Path(args.out_path))
    print(json.dumps(meta, indent=2))
```

Usage in orchestration (Mac):

```bash
# 1) Project today's enriched parquet to canonical shards
python project_parquet.py \
  --in batches/mirror-merged/2026-05-03/enriched/part-0000.parquet \
  --out training_shards/2026-05-03-canonical/part-0000.parquet

# 2) Build manifest for canonical folder (one HF API call)
python build_manifest.py \
  --repo datasets/my-data \
  --folder training_shards/2026-05-03-canonical \
  --out manifests/2026-05-03.json
```

---

## 3. CDN-only training loader (no HF API at runtime)

```python
# /opt/axentx/vanguard/backend/train_cdn_only.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Any

import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import Dataset, DataLoader

from manifest import Manifest


class CDNParquetDataset(Dataset):
    """
    Load parquet shards directly from CDN using pinned manifest.
    No HF API calls during training.
    """

    def __init__(self, manifest_path: Path, cache_dir: Optional[Path] = None):
        self.manifest = Manifest.load(manifest_path)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
           
