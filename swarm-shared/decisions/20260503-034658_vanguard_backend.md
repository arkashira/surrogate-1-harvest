# vanguard / backend

## 1. Diagnosis

- No deterministic manifest exists: training scripts likely call `load_dataset` or `list_repo_files` at runtime, triggering HF API rate limits and breaking reproducibility.
- No CDN-only data path: ingestion probably writes mixed-schema files into `enriched/` with extra metadata columns, violating the surrogate-1 schema contract and bloating storage.
- No studio reuse / idle-stop handling: orchestration likely recreates Lightning studios or fails when idle timeouts stop running instances, wasting quota and interrupting long jobs.
- No content-addressed artifact layout: file naming probably lacks date/slug determinism, making incremental ingestion and caching hard to reason about.
- No zero-runtime-HF-API guarantee: training code probably still depends on `datasets` or `list_repo_*` during DataLoader init, risking 429s in long runs.

## 2. Proposed change

Add a minimal, production-grade ingestion + training contract under `/opt/axentx/vanguard/backend/`:

- `backend/ingest/manifest.py` — produce `batches/mirror-merged/{date}/{slug}.json` listing only CDN-resolvable file paths for that slug/date.
- `backend/ingest/project.py` — download raw files via HF CDN (no auth), project to `{prompt, response}`, and write `batches/mirror-merged/{date}/{slug}.parquet` (no extra columns).
- `backend/train/loader.py` — DataLoader that uses the manifest and CDN URLs only (zero HF API calls during training).
- `backend/orchestrator/studio.py` — Lightning studio reuse + idle-stop handling + deterministic machine fallback (L40S → public tier).

Scope: add these four files and wire a single CLI entrypoint `backend/run_ingest.py` for one slug/date.

## 3. Implementation

```bash
# create structure
mkdir -p /opt/axentx/vanguard/backend/{ingest,train,orchestrator}
```

### backend/ingest/manifest.py
```python
from __future__ import annotations
import json
from datetime import date
from pathlib import Path
from huggingface_hub import HfApi, list_repo_tree

HF_REPO = "datasets/axentx/mirror-merged"

def build_manifest(target_date: date, slug: str) -> dict:
    """
    Returns:
      {
        "date": "YYYY-MM-DD",
        "slug": "slug",
        "files": [
          "YYYY-MM-DD/slug/file1.jsonl",
          ...
        ],
        "cdn_urls": [
          "https://huggingface.co/datasets/axentx/mirror-merged/resolve/main/YYYY-MM-DD/slug/file1.jsonl",
          ...
        ]
      }
    """
    prefix = f"{target_date.isoformat()}/{slug}"
    api = HfApi()
    # single shallow list per folder to minimize API calls
    entries = list_repo_tree(repo_id=HF_REPO, path=prefix, repo_type="dataset", recursive=False)
    files = [e.path for e in entries if e.type == "file"]

    cdn_urls = [
        f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{f}"
        for f in files
    ]

    manifest = {
        "date": target_date.isoformat(),
        "slug": slug,
        "files": files,
        "cdn_urls": cdn_urls,
    }

    out_dir = Path(__file__).parent.parent.parent / "batches" / "mirror-merged" / target_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest

if __name__ == "__main__":
    import sys
    d, s = sys.argv[1], sys.argv[2]
    build_manifest(date.fromisoformat(d), s)
```

### backend/ingest/project.py
```python
from __future__ import annotations
import pyarrow as pa
import pyarrow.parquet as pq
import json
import requests
from pathlib import Path
from typing import Iterator

SCHEMA = pa.schema([
    pa.field("prompt", pa.string()),
    pa.field("response", pa.string()),
])

def cdn_lines(url: str) -> Iterator[str]:
    # CDN download: no Authorization header -> bypasses API rate limits
    with requests.get(url, timeout=60) as r:
        r.raise_for_status()
        # streaming decode for large files
        for line in r.iter_lines(decode_unicode=True):
            if line:
                yield line

def extract_prompt_response(raw: dict) -> dict:
    # Best-effort projection; adapt keys per corpus as needed
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def project_manifest(manifest_path: str | Path) -> Path:
    manifest = json.loads(Path(manifest_path).read_text())
    rows = []
    for url in manifest["cdn_urls"]:
        for line in cdn_lines(url):
            try:
                raw = json.loads(line)
            except Exception:
                continue
            rows.append(extract_prompt_response(raw))

    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    out_path = Path(manifest_path).with_suffix(".parquet")
    pq.write_table(table, out_path)
    return out_path

if __name__ == "__main__":
    import sys
    project_manifest(sys.argv[1])
```

### backend/train/loader.py
```python
from __future__ import annotations
import json
import pyarrow.parquet as pq
import torch
from torch.utils.data import Dataset, DataLoader

class CDNParquetDataset(Dataset):
    def __init__(self, parquet_path: str):
        self.table = pq.read_table(parquet_path, columns=["prompt", "response"])
        self.prompts = self.table["prompt"].to_pylist()
        self.responses = self.table["response"].to_pylist()

    def __len__(self) -> int:
        return len(self.prompts)

    def __getitem__(self, idx: int) -> dict:
        return {
            "prompt": self.prompts[idx],
            "response": self.responses[idx],
        }

def make_loader(parquet_path: str, batch_size: int = 8, num_workers: int = 0) -> DataLoader:
    ds = CDNParquetDataset(parquet_path)
    return DataLoader(ds, batch_size=batch_size, shuffle=True, num_workers=num_workers)
```

### backend/orchestrator/studio.py
```python
from lightning import Studio, Teamspace, Machine
from typing import Optional

def get_or_create_studio(
    name: str,
    machine: Machine = Machine.L40S,
    fallback_machine: Machine = Machine.L40S,
) -> Studio:
    # reuse running studio to save quota
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            return s

    # if stopped, restart on same machine; fallback if unavailable
    try:
        studio = Studio(name=name, create_ok=True, machine=machine)
    except Exception:
        studio = Studio(name=name, create_ok=True, machine=fallback_machine)
    return studio

def run_with_idle_guard(studio: Studio, target, *args, **kwargs):
    # Lightning idle stop kills training; restart if stopped
    if studio.status != "Running":
        studio.start(machine=studio.machine)
    return studio.run(target, *args, **kwargs)
```

### backend/run_ingest.py
```python
from pathlib import Path
from vanguard.backend.ingest.manifest import build_manifest
from vanguard.backend.ingest.project import project_manifest

def main(date_str: str, slug: str):
    manifest = build_manifest(date_str, slug)
    manifest_path = Path(__file__).parent.parent / "batches" / "mirror-merged" / date_str / f"{slug}.json"
    project_manifest(manifest_path)
    print(f"Created parquet for {date_str}/{slug}")

if __name__ == "__main__":
    import sys
    main(sys.argv[1], sys.argv[2])
```

## 4. Verification

1. Build manifest (single API call
