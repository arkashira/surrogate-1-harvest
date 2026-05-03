# airship / discovery

## Incremental Improvement: Manifest-Driven CDN-Only Dataset Loader (Discovery Focus)

**Highest-value 2h ship**: Replace the current `load_dataset`/`list_repo_files` ingestion path with a manifest-driven, CDN-only loader that eliminates HF API 429s and `pyarrow.CastError` from mixed-schema repos. This unblocks surrogate-1 training and aligns with the known patterns (HF CDN bypass, pre-list once, project-at-parse).

**Why now**: Discovery phase needs reliable data access for knowledge-rag/graph queries; current ingestion is rate-limited and brittle. This change is isolated to the loader and can ship without touching models or infra.

---

## Implementation Plan (≤2h)

1. **Create manifest generator** (`scripts/build_dataset_manifest.py`)
   - Runs on Mac (or CI) after rate-limit window.
   - Uses `list_repo_tree(path, recursive=False)` per date folder.
   - Emits `manifests/{repo}/{date}_manifest.json` with `{file_path, size, sha, url}`.
   - Embeds CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`).

2. **Create CDN-only iterable dataset** (`airship/data/cdn_dataset.py`)
   - Accepts manifest path.
   - Streams files via `requests.get(url, timeout=30)` with retry/backoff.
   - Parses per-file: project to `{prompt, response}` only at parse time (ignore extra cols).
   - Yields dicts; optionally filters by schema signature.

3. **Update ingestion entrypoint** (`airship/ingest/run.py`)
   - If manifest exists, use `CdnDataset`; else fallback to legacy with warning.
   - Write enriched output to `batches/mirror-merged/{date}/{slug}.parquet` (no `source`/`ts` cols).

4. **Add lightweight tests & docs**
   - One unit test for manifest loading and CDN fetch mock.
   - Update README section with usage and rate-limit notes.

5. **Verify end-to-end**
   - Run manifest build on a small date folder.
   - Run ingestion → parquet.
   - Confirm no API calls during data load (check logs).

---

## Code Snippets

### 1. Manifest Generator
```python
# scripts/build_dataset_manifest.py
#!/usr/bin/env python3
"""
Build a CDN-only manifest for a HuggingFace dataset repo.
Usage:
  python build_dataset_manifest.py <repo> <date_folder> --out-dir ./manifests
"""
import argparse
import json
import os
import time
from pathlib import Path
from typing import List, Dict

from huggingface_hub import HfApi

API = HfApi()
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date_folder: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{date_folder}/"
    items = API.list_repo_tree(repo=repo, path=prefix, recursive=False)

    entries = []
    for item in items:
        if item.type != "file":
            continue
        entries.append({
            "file_path": item.path,
            "size": item.size,
            "sha": getattr(item, "oid", None),
            "url": CDN_TEMPLATE.format(repo=repo, path=item.path),
        })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entries": entries,
    }

    out_path = out_dir / f"{repo}_{date_folder}_manifest.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path} ({len(entries)} files)")
    return out_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN-only dataset manifest.")
    parser.add_argument("repo", help="HF dataset repo (e.g., 'myorg/mydata')")
    parser.add_argument("date_folder", help="Date folder in dataset (e.g., '2026-04-29')")
    parser.add_argument("--out-dir", default="./manifests", help="Output directory")
    args = parser.parse_args()
    build_manifest(args.repo, args.date_folder, Path(args.out_dir))
```

### 2. CDN-Only Iterable Dataset
```python
# airship/data/cdn_dataset.py
import json
import time
from pathlib import Path
from typing import Iterator, Dict, Any

import requests
from requests.adapters import HTTPAdapter, Retry

from airship.data.parse import project_to_prompt_response  # implements projection logic

class CdnDataset:
    """
    Iterable dataset that reads files listed in a manifest via CDN URLs.
    No HuggingFace API calls during iteration.
    """

    def __init__(self, manifest_path: Path, max_retries: int = 3, timeout: int = 30):
        self.manifest_path = Path(manifest_path)
        self.manifest = json.loads(self.manifest_path.read_text())
        self.timeout = timeout
        self.session = self._build_session(max_retries)

    def _build_session(self, max_retries: int) -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=max_retries,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        session.mount("https://", HTTPAdapter(max_retries=retries))
        return session

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for entry in self.manifest["entries"]:
            url = entry["url"]
            try:
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                # delegate projection to parse module; ignore mixed extra fields
                record = project_to_prompt_response(resp.content, entry["file_path"])
                if record is None:
                    continue
                yield record
            except Exception as exc:
                # log and continue to avoid breaking entire iterable
                print(f"Failed to fetch {url}: {exc}")
                continue

    def __len__(self) -> int:
        return len(self.manifest["entries"])
```

### 3. Projection Helper (minimal)
```python
# airship/data/parse.py
import json
from typing import Optional, Dict, Any

def project_to_prompt_response(content: bytes, file_path: str) -> Optional[Dict[str, Any]]:
    """
    Project raw file content to {prompt, response}.
    Supports JSON/JSONL lines and simple text heuristics.
    Returns None if projection fails.
    """
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None

    # If JSON/JSONL, try common field names
    try:
        data = json.loads(text)
        # If it's a list of records, iterate (simplified: take first)
        if isinstance(data, list) and len(data) > 0:
            data = data[0]

        if isinstance(data, dict):
            prompt = data.get("prompt") or data.get("input") or data.get("question")
            response = data.get("response") or data.get("output") or data.get("answer")
            if prompt is not None and response is not None:
                return {"prompt": str(prompt), "response": str(response), "source_file": file_path}
    except json.JSONDecodeError:
        pass

    # Fallback: simple newline split for conversational pairs
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 2:
        return {"prompt": lines[0], "response": lines[1], "source_file": file_path}

    return None
```

### 4. Updated Ingestion Entrypoint (excerpt)
```python
# airship/ingest/run.py
from pathlib import Path
from airship.data.cdn_dataset import CdnDataset

def run_ingest(repo: str, date_folder: str, out_dir: Path):
    manifest_path = Path("manifests") / f"{repo}_{date_folder}_manifest.json"
    if manifest_path.exists():
        dataset = CdnDataset(manifest_path)
        print("Using CDN-only dataset (manifest-driven).")
    else:
        raise
