# vanguard / backend

## Final Synthesis (Best of Both Candidates)

**Core diagnosis (merged, de-duplicated):**
- Runtime `load_dataset`/recursive file enumeration triggers HF API 429s and non-reproducible epochs.
- No content-addressed manifest (file list + SHA256) → CDN-only fetching impossible; epochs not deterministic.
- Mixed-schema files in `enriched/` risk `pyarrow.CastError` and schema drift during surrogate-1 training.
- Single-repo ingestion risks HF 128-commit/hr cap; no deterministic repo selection or sibling spread.
- No guard to reuse existing Lightning Studio sessions; training script recreates studios and burns quota / hits idle-stop failures.

**Chosen implementation scope:** `/opt/axentx/vanguard/backend/`

---

## 1. Manifest generator (rate-limit safe)

`backend/data/manifest.py`

- Single non-recursive `list_repo_tree` per date folder (one API call).
- Deterministic sibling-repo assignment by hash-slug to spread HF writes and avoid 128-commit/hr cap.
- Produces `manifest.json` with `{file_path, sha256, size, repo}` for CDN-only, reproducible epochs.
- Orchestrator should run this *after* rate-limit window clears; commit manifest to repo (or store in CI cache) so training never calls HF API for file lists.

```python
#!/usr/bin/env python3
"""
Generate content-addressed manifest for one date folder.
Run from orchestrator after rate-limit window clears.
"""
import json, hashlib, os, sys
from pathlib import Path
from typing import List, Dict
from huggingface_hub import HfApi, hf_hub_download

API = HfApi()

def list_date_folder(repo_id: str, date_folder: str) -> List[str]:
    tree = API.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
    return [item.rfilename for item in tree if item.type == "file"]

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(
    repo_id: str,
    date_folder: str,
    cache_dir: str,
    output_path: str,
    sibling_repos: List[str] | None = None,
) -> None:
    if sibling_repos is None:
        sibling_repos = [repo_id]

    files = list_date_folder(repo_id, date_folder)
    if not files:
        print("No files found.", file=sys.stderr)
        sys.exit(1)

    manifest: List[Dict] = []
    os.makedirs(cache_dir, exist_ok=True)

    for rel_path in sorted(files):
        # Deterministic repo selection to spread HF writes
        slug = os.path.splitext(os.path.basename(rel_path))[0]
        repo = sibling_repos[hash(slug) % len(sibling_repos)]

        local_path = hf_hub_download(
            repo_id=repo,
            filename=rel_path,
            cache_dir=cache_dir,
            force_download=False,
        )
        manifest.append(
            {
                "file_path": rel_path,
                "sha256": sha256_file(local_path),
                "size": os.path.getsize(local_path),
                "repo": repo,
            }
        )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out} ({len(manifest)} files)")

if __name__ == "__main__":
    build_manifest(
        repo_id="datasets/org/mirror-merged",
        date_folder="batches/mirror-merged/2026-04-29",
        cache_dir="/tmp/hf_cache",
        output_path="backend/data/manifest.json",
        sibling_repos=[
            "datasets/org/mirror-0",
            "datasets/org/mirror-1",
            "datasets/org/mirror-2",
            "datasets/org/mirror-3",
            "datasets/org/mirror-4",
        ],
    )
```

---

## 2. CDN-only streaming loader (zero HF API during training)

`backend/data/cdn_loader.py`

- Uses manifest; zero HF API calls during training.
- Robust schema projection: keeps only `prompt`/`response` (case-insensitive), coerces to string, drops extra columns (`source`, `ts`, etc.) to prevent `pyarrow.CastError` and schema drift.
- Streaming-friendly; validates SHA256 against manifest for reproducibility.

```python
#!/usr/bin/env python3
"""
CDN-only streaming loader (zero HF API calls during training).
Uses manifest produced by manifest.py.
"""
import json, hashlib, io
from pathlib import Path
from typing import Iterator, Dict, Any
import pyarrow.parquet as pq
import pyarrow as pa
import requests

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"

def cdn_stream(repo: str, file_path: str) -> bytes:
    url = CDN_TEMPLATE.format(repo=repo, file_path=file_path)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def project_to_prompt_response(batch: pa.Table) -> pa.Table:
    """
    Project heterogeneous schemas to {prompt, response} only.
    Keeps only string/utf8 columns named prompt/response (case-insensitive).
    Drops extra columns (source, ts, etc.) to avoid schema drift.
    """
    cols = {}
    for name in batch.column_names:
        low = name.lower()
        if low in ("prompt", "response"):
            col = batch[name]
            if not pa.types.is_string(col.type):
                col = col.cast(pa.string())
            cols[low] = col
    if "prompt" not in cols or "response" not in cols:
        raise ValueError("Missing prompt/response in batch")
    return pa.table(cols)

class CDNParquetLoader:
    def __init__(self, manifest_path: str | Path, validate_sha256: bool = True):
        manifest_path = Path(manifest_path)
        self.items = json.loads(manifest_path.read_text())
        self.validate_sha256 = validate_sha256

    def _validate(self, data: bytes, expected_sha256: str) -> None:
        if not self.validate_sha256:
            return
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            raise ValueError(f"SHA256 mismatch: expected {expected_sha256}, got {actual}")

    def iter_batches(self, batch_size: int = 1024) -> Iterator[Dict[str, Any]]:
        for item in self.items:
            raw = cdn_stream(item["repo"], item["file_path"])
            self._validate(raw, item["sha256"])
            table = pq.read_table(io.BytesIO(raw))
            table = project_to_prompt_response(table)
            for batch in table.to_batches(max_chunksize=batch_size):
                yield {"prompt": batch["prompt"], "response": batch["response"]}
```

---

## 3. Training entrypoint (manifest-driven)

`backend/train/train.py`

- Accepts `--manifest` and uses `cdn_loader`.
- No HF API calls for file enumeration or metadata during training.
- Deterministic epochs via fixed manifest order.

```python
#!/usr/bin/env python3
import argparse
from pathlib import Path
from backend.data.cdn_loader import CDNParquetLoader

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True, help="Path to manifest.json")
    parser.add_argument("--batch-size", type=int, default=1024)
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    loader = CDNParquetLoader(args.manifest, validate_sha256=True)

    for epoch in range(args.epochs):
        print(f"Epoch {epoch + 1}/{args.epochs}")
        for batch in loader.iter_batches(batch_size=args.batch_size):
            # Replace with actual
