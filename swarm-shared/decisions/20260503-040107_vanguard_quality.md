# vanguard / quality

## 1. Diagnosis
- No content-addressed manifest: ingestion/training re-lists HF repos at runtime → 429 rate-limits and non-reproducible runs.
- Mixed-schema files from `dataset-mirror` land in `enriched/` without projection to `{prompt,response}` → breaks surrogate-1 training pipelines.
- No CDN-only data strategy: training scripts still make HF API calls during data loading instead of using pre-listed CDN URLs.
- Missing schema guardrails: ingestion accepts heterogeneous files and writes extra cols (`source`, `ts`) that violate surrogate-1 expected schema.
- No lightweight manifest validation: no checksum or schema check before training starts, so corrupted/partial batches surface late.

## 2. Proposed change
Create `/opt/axentx/vanguard/ingest/manifest.py` (new) and update the mirror-to-parquet writer (likely `/opt/axentx/vanguard/ingest/mirror.py` or equivalent) to:
- Produce a content-addressed manifest JSON per batch: `{date}/{slug}.json` with `{"slug": "...", "sha256": "...", "files": [...], "projected_schema": {"prompt": "str", "response": "str"}, "n_rows": N}`.
- Project to `{prompt,response}` only and drop extra cols before writing `batches/mirror-merged/{date}/{slug}.parquet`.
- Embed the file list in the manifest so training can use CDN-only fetches with zero API calls.

## 3. Implementation

```bash
# /opt/axentx/vanguard/ingest/manifest.py
#!/usr/bin/env python3
"""
Content-addressed manifest generator for surrogate-1 ingestion.
Produces manifest JSON and enforces {prompt,response}-only schema.
"""
import json
import hashlib
import os
from pathlib import Path
from typing import Dict, List, Any
import pyarrow as pa
import pyarrow.parquet as pq

MANIFEST_DIR = Path(os.getenv("VANGUARD_MANIFEST_DIR", "batches/mirror-merged"))

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def project_to_prompt_response(table: pa.Table) -> pa.Table:
    """Keep only prompt/response; coerce to string; drop extra cols."""
    cols = set(table.column_names)
    required = {"prompt", "response"}
    if not required.issubset(cols):
        missing = required - cols
        # Best-effort: try common aliases
        aliases = {
            "instruction": "prompt",
            "input": "prompt",
            "output": "response",
            "completion": "response",
        }
        for old, new in aliases.items():
            if old in cols and new not in cols:
                table = table.append_column(new, table[old].cast(pa.string()))
        cols = set(table.column_names)
        if not required.issubset(cols):
            raise ValueError(f"Missing required columns after aliasing: {missing}")
    keep = [c for c in table.column_names if c in required]
    table = table.select(keep).cast(pa.schema([pa.field("prompt", pa.string()), pa.field("response", pa.string())]))
    return table

def write_batch_parquet_and_manifest(
    rows: List[Dict[str, str]],
    date: str,
    slug: str,
    base_dir: Path = Path("batches/mirror-merged"),
) -> Dict[str, Any]:
    """Write projected parquet + manifest; return manifest dict."""
    base = base_dir / date
    base.mkdir(parents=True, exist_ok=True)

    table = pa.Table.from_pylist(rows, schema=pa.schema([
        pa.field("prompt", pa.string()),
        pa.field("response", pa.string()),
    ]))
    table = project_to_prompt_response(table)

    parquet_path = base / f"{slug}.parquet"
    pq.write_table(table, parquet_path)

    manifest = {
        "slug": slug,
        "date": date,
        "parquet": str(parquet_path),
        "sha256": sha256_file(parquet_path),
        "n_rows": table.num_rows,
        "projected_schema": {"prompt": "str", "response": "str"},
        "files": [str(parquet_path)],
    }

    manifest_path = base / f"{slug}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest

if __name__ == "__main__":
    # Minimal smoke test
    sample = [
        {"prompt": "Explain Python imports", "response": "Use import or from...", "extra": "drop me"},
        {"prompt": "What is QA?", "response": "Quality assurance.", "source": "wiki"},
    ]
    out = write_batch_parquet_and_manifest(sample, "2026-05-03", "test-smoke")
    print(json.dumps(out, indent=2))
```

```bash
# If a mirror writer exists, patch it to use the manifest writer.
# Example snippet to replace/enhance existing write path:
#
# from vanguard.ingest.manifest import write_batch_parquet_and_manifest
#
# def write_mirror_batch(rows, date, slug):
#     # rows may contain extra fields; manifest writer projects.
#     manifest = write_batch_parquet_and_manifest(rows, date, slug)
#     return manifest
```

```bash
# /opt/axentx/vanguard/train/train.py  (or data loader section)
# Embed pre-listed CDN file list to avoid runtime HF API calls.
#
# Example loader snippet:
#
# import json
# from pathlib import Path
# import pyarrow.parquet as pq
# import requests
#
# def load_from_cdn(manifest_path: str):
#     manifest = json.loads(Path(manifest_path).read_text())
#     parquet_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{manifest['parquet']}"
#     # CDN fetch — no Authorization header, bypasses API rate limits.
#     with requests.get(parquet_url, timeout=60) as r:
#         r.raise_for_status()
#         with open("/tmp/batch.parquet", "wb") as f:
#             f.write(r.content)
#     return pq.read_table("/tmp/batch.parquet").to_pylist()
```

## 4. Verification
1. Run smoke test:
   ```bash
   cd /opt/axentx/vanguard/ingest && python3 manifest.py
   ```
   Confirm `batches/mirror-merged/2026-05-03/test-smoke.parquet` exists and `test-smoke.json` contains `n_rows: 2` and only `prompt/response` schema.

2. Validate projection:
   ```bash
   python3 -c "
import pyarrow.parquet as pq
t = pq.read_table('batches/mirror-merged/2026-05-03/test-smoke.parquet')
print('cols:', t.column_names)
print('rows:', t.num_rows)
   "
   ```
   Output must be `cols: ['prompt', 'response']` and `rows: 2`.

3. CDN fetch test (simulate training loader):
   ```bash
   python3 -c "
import requests, json, tempfile, pyarrow.parquet as pq
m = json.loads(open('batches/mirror-merged/2026-05-03/test-smoke.json').read())
# If repo is public, try CDN fetch pattern (adjust HF_REPO as needed).
print('Manifest OK:', m['slug'], m['sha256'][:8])
   "
   ```

4. Integration check: run existing ingestion/mirror script (or a small slice) and confirm:
   - No extra columns (`source`, `ts`) appear in output parquet.
   - A `.json` manifest is produced alongside each `.parquet`.
   - Training script can be pointed at the manifest and perform CDN-only fetches (zero HF API calls during data load).
