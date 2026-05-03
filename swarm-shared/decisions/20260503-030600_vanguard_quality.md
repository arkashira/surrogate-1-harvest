# vanguard / quality

## Final Synthesized Implementation (Best of Both Candidates)

**Diagnosis (Resolved):**
- **CDN-first determinism**: Both candidates agree we must eliminate recursive `list_repo_tree` and `load_dataset` calls that cause 429s. Use **single non-recursive directory listing** + **CDN URLs**.
- **Integrity**: Mandatory **SHA-256** verification for every file to prevent silent corruption during long training runs.
- **Schema drift**: Downstream contract must strictly project to `{prompt, response}`; ignore extra columns (`source`, `ts`) at load time, never persist them.
- **Reproducibility**: Manifest must be a **static snapshot** (paths, sizes, hashes, Parquet metadata) keyed by date folder. Validate against live repo state before training starts.

**Implementation (Combined & Hardened):**

```bash
# /opt/axentx/vanguard/manifest.py
#!/usr/bin/env python3
"""
CDN-first manifest builder & validator for HuggingFace datasets.
Guarantees:
- Deterministic snapshot (no recursive API calls)
- SHA-256 integrity verification
- Parquet metadata capture (rows, row groups, schema digest)
- Strict {prompt,response} projection at load time
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, hf_hub_download, model_info

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
METADATA_CHUNK = 1024 * 1024  # 1 MiB for Parquet metadata sniff

@dataclass(order=True)
class ParquetMeta:
    num_rows: int
    num_row_groups: int
    schema_digest: str  # sha256 of canonical Arrow schema string

    @classmethod
    def from_file(cls, path: Path) -> "ParquetMeta":
        pf = pq.ParquetFile(path)
        schema_str = pf.schema_arrow.to_string()
        return cls(
            num_rows=pf.metadata.num_rows,
            num_row_groups=pf.metadata.num_row_groups,
            schema_digest=hashlib.sha256(schema_str.encode()).hexdigest(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ParquetMeta":
        return cls(**d)

@dataclass
class ManifestEntry:
    path: str
    cdn_url: str
    size_bytes: int
    sha256: str
    parquet: ParquetMeta

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["parquet"] = self.parquet.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ManifestEntry":
        d["parquet"] = ParquetMeta.from_dict(d["parquet"])
        return cls(**d)

def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()

def repo_exists(repo: str) -> bool:
    try:
        model_info(repo, token=False)  # datasets repo shows as model info
        return True
    except Exception:
        return False

def build_manifest(
    repo: str,
    date: str,
    out_path: Path,
    cache_root: Path,
    force: bool = False,
) -> None:
    if not repo_exists(repo):
        print(f"Error: repo '{repo}' not found or inaccessible.", file=sys.stderr)
        sys.exit(1)

    api = HfApi()
    cache_root.mkdir(parents=True, exist_ok=True)

    # Single non-recursive API call (avoids pagination + 429s)
    items = api.list_repo_tree(repo=repo, path=date, recursive=False)
    file_items = sorted(
        (it for it in items if it.type == "file" and it.path.endswith(".parquet")),
        key=lambda it: it.path,
    )
    if not file_items:
        print(f"No parquet files found in {repo}/{date}", file=sys.stderr)
        sys.exit(1)

    entries: List[ManifestEntry] = []
    for item in file_items:
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=item.path)
        # Flatten folder structure in cache to avoid deep nesting issues
        safe_name = item.path.replace("/", "_")
        local_path = cache_root / safe_name

        if local_path.exists() and not force:
            sz = local_path.stat().st_size
            if sz == item.size:
                print(f"Found cached: {item.path}", file=sys.stderr)
            else:
                print(f"Size mismatch, re-downloading: {item.path}", file=sys.stderr)
                local_path.unlink(missing_ok=True)

        if not local_path.exists():
            print(f"Downloading (CDN): {item.path}", file=sys.stderr)
            try:
                local_path = Path(
                    hf_hub_download(
                        repo_id=repo,
                        filename=item.path,
                        cache_dir=cache_root,
                        local_dir=cache_root,
                        local_dir_use_symlinks=False,
                    )
                )
            except Exception as exc:
                print(f"Download failed for {item.path}: {exc}", file=sys.stderr)
                sys.exit(1)

        # Integrity + Parquet metadata
        sz = local_path.stat().st_size
        digest = sha256_file(local_path)
        pq_meta = ParquetMeta.from_file(local_path)

        entries.append(
            ManifestEntry(
                path=item.path,
                cdn_url=cdn_url,
                size_bytes=sz,
                sha256=digest,
                parquet=pq_meta,
            )
        )

    manifest = {
        "repo": repo,
        "date": date,
        "generated_by": "vanguard/manifest.py",
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "entries": [e.to_dict() for e in entries],
    }
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path}")

def validate_manifest(manifest_path: Path, cache_root: Path) -> bool:
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:
        print(f"Invalid manifest JSON: {exc}", file=sys.stderr)
        return False

    ok = True
    for raw in manifest.get("entries", []):
        e = ManifestEntry.from_dict(raw)
        local_path = cache_root / e.path.replace("/", "_")

        if not local_path.exists():
            print(f"Missing: {e.path}", file=sys.stderr)
            ok = False
            continue

        if local_path.stat().st_size != e.size_bytes:
            print(f"Size mismatch: {e.path}", file=sys.stderr)
            ok = False
            continue

        if sha256_file(local_path) != e.sha256:
            print(f"SHA256 mismatch: {e.path}", file=sys.stderr)
            ok = False
            continue

        try:
            live = ParquetMeta.from_file(local_path)
            if live.num_rows != e.parquet.num_rows or live.schema_digest != e.parquet.schema_digest:
                print(f"Parquet metadata mismatch: {e.path}", file=sys.stderr)
                ok = False
        except Exception as exc:
            print(f"Parquet read error {e.path}: {exc}", file=sys.stderr)
            ok = False

    if ok:
        print("All entries validated OK.")
    return ok

def project_prompt_response(parquet_path: Path, output_path: Path) -> None:
    """
    Strict projection to {prompt, response}.
    Drops all other columns to enforce downstream contract.
    """
    import pyarrow.compute as pc
    table = pq.read_table(parquet_path)
    # Keep only
