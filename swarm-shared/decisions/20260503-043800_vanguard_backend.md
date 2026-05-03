# vanguard / backend

## Final synthesized solution (best parts, contradictions resolved)

**Core diagnosis (agreed across candidates)**
- Runtime repo enumeration during training causes HF API 429s and non-reproducible epochs.
- No content-addressed `{path, sha256}` snapshot → CDN fetches can’t be validated or resumed reliably.
- No ingestion safeguard to normalize heterogeneous files to a strict `{prompt, response}` schema before upload → downstream `pyarrow.CastError` risk.
- No Lightning idle-stop / reuse handling → quota waste and training death on idle timeout.

**Chosen approach**
- One new file `/opt/axentx/vanguard/backend/manifest.py` that:
  - On orchestrator/Mac: single `list_repo_tree(recursive=False)` on a date folder → deterministic `manifest-{date}.json` with `{path, sha256, size}`.
  - Provides a CDN-only `DataLoader`/`stream_parquet_shards` that fetches via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` with zero Authorization header and validates `sha256` on download.
  - Exposes `validate_manifest`, `stream_parquet_shards`, and a small schema-projection/normalization step to enforce `{prompt, response}` before yielding batches.
- Patch the training script to import and use the manifest (no recursive API calls during training).
- Add a lightweight CLI to produce the manifest and an optional ingestion safeguard to reject non-conforming files at manifest-build time.
- Add Lightning idle-stop handling (Studio settings or script-level timeout + checkpoint resume) to avoid quota waste.

**Resolved contradictions in favor of correctness + actionability**
- Use `list_repo_tree(recursive=False)` (not recursive) and download each file once to compute `sha256` (not rely on server-provided hashes that may be missing or non-sha256). This is correct and reproducible.
- Keep CDN fetches without Authorization to bypass `/api/` rate limits, but validate local checksums before use. This is safe and performant.
- Enforce schema at manifest-build time (fail fast) and during streaming (project columns) to prevent `pyarrow.CastError`. This is robust.
- Do not embed large manifests in training scripts; commit or version them alongside code so Lightning runs are CDN-only and reproducible.

---

## 1. Implementation

### `/opt/axentx/vanguard/backend/manifest.py`
```python
#!/usr/bin/env python3
"""
Content-addressed manifest generator + CDN-only loader for HF datasets.

Usage (orchestrator):
    python -m vanguard.backend.manifest \
        --repo datasets/elicit/surrogate-1 \
        --date 2026-04-29 \
        --out manifests/manifest-2026-04-29.json

Training (Lightning):
    from vanguard.backend.manifest import stream_parquet_shards
    for batch in stream_parquet_shards(manifest_path, repo=REPO, batch_size=512):
        ...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, hf_hub_download

API = HfApi()
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


@dataclass(order=True)
class FileEntry:
    path: str
    sha256: str
    size: int


def sha256_file(path: str, chunk_kb: int = 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_kb * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_to_prompt_response(table: pa.Table) -> pa.Table:
    """
    Project/cast table to strict {prompt, response} schema.
    Raises if required columns are missing or cannot be cast to string.
    """
    required = {"prompt", "response"}
    missing = required - set(table.column_names)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    # Cast to string to avoid pyarrow.CastError downstream
    prompt_col = table.column("prompt").cast(pa.string())
    response_col = table.column("response").cast(pa.string())
    return pa.table({"prompt": prompt_col, "response": response_col})


def build_manifest(
    repo: str,
    date_folder: str,
    out_path: str,
    require_schema: bool = True,
) -> List[FileEntry]:
    """
    Single API call: list one date folder (non-recursive) and snapshot entries.
    Downloads each file once to compute sha256, validates schema if requested.
    """
    entries: List[FileEntry] = []
    items = API.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [i for i in items if i.type == "file" and i.path.endswith(".parquet")]

    cache_dir = Path.home() / ".cache" / "vanguard" / "hf_downloads"
    cache_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        local = hf_hub_download(
            repo_id=repo,
            filename=f.path,
            cache_dir=str(cache_dir),
            force_download=False,
        )
        if require_schema:
            try:
                table = pq.read_table(local, columns=["prompt", "response"])
                _normalize_to_prompt_response(table)
            except Exception as exc:
                raise ValueError(
                    f"Schema validation failed for {f.path}: {exc}"
                ) from exc

        entries.append(
            FileEntry(
                path=f.path,
                sha256=sha256_file(local),
                size=os.path.getsize(local),
            )
        )

    manifest = [asdict(e) for e in entries]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(entries)} entries to {out_path}")
    return entries


def load_manifest(path: str) -> List[FileEntry]:
    raw = json.loads(Path(path).read_text())
    return [FileEntry(**item) for item in raw]


def validate_file(entry: FileEntry, local_path: str) -> bool:
    return sha256_file(local_path) == entry.sha256


def stream_parquet_shards(
    manifest_path: str,
    repo: str,
    batch_size: int = 512,
    cache_dir: Optional[str] = None,
    timeout: int = 120,
) -> Iterator[Dict[str, list]]:
    """
    CDN-only fetches (no Authorization header) + sha256 validation.
    Yields Arrow tables projected to {prompt, response} and batched.
    """
    entries = load_manifest(manifest_path)
    if cache_dir is None:
        cache_dir = str(Path.home() / ".cache" / "vanguard" / "hf_downloads")
    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    for entry in entries:
        local = os.path.join(cache_dir, os.path.basename(entry.path))
        if not os.path.exists(local) or not validate_file(entry, local):
            url = CDN_TEMPLATE.format(repo=repo, path=entry.path)
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            Path(local).write_bytes(r.content)
            if not validate_file(entry, local):
                raise RuntimeError(f"Checksum mismatch: {entry.path}")

        table = pq.read_table(local, columns=["prompt", "response"])
        table = _normalize_to_prompt_response(table)
        df = table.to_pandas()
        for i in range(0, len(df), batch_size):
            batch = df.iloc[i : i + batch_size]
            yield {"prompt": batch["prompt"].tolist(), "response": batch["response"].tolist()}


def validate_manifest(repo: str, manifest_path: str) -> Tuple[bool, List[str]]:
    """
    Validate that all entries in manifest exist on CDN and checksums match.
    Returns (all_ok,
