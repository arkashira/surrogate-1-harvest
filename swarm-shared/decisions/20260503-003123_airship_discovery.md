# airship / discovery

## Final Unified Implementation Plan (≤2h)

**Goal**: Eliminate Hugging Face API rate limits and mixed-schema ingestion failures in surrogate training by switching to a manifest-driven, CDN-only iterable loader.

**Core decision**: Use Candidate 1’s complete, working code as the baseline (manifest builder + `CdnDataset` + parser + training integration). Adopt Candidate 2’s parquet-specific optimization as an optional fast-path inside the parser. Resolve all contradictions in favor of correctness and concrete actionability.

---

### 1) Manifest builder (unchanged, production-ready)
Run on the Mac orchestrator. One `list_repo_tree` call per date folder; outputs `file_manifest.json` with CDN URLs.

```python
# scripts/build_file_manifest.py
#!/usr/bin/env python3
"""
Generate file_manifest.json for a single date folder in a HF dataset repo.
Usage:
  HF_REPO=datasets/your/repo FOLDER=2026-05-03 python build_file_manifest.py > file_manifest.json
"""
import os
import json
import sys
from datetime import datetime
from huggingface_hub import HfApi

HF_REPO = os.environ.get("HF_REPO")
FOLDER = os.environ.get("FOLDER")  # e.g. 2026-05-03

if not HF_REPO or not FOLDER:
    print("Set HF_REPO and FOLDER env vars", file=sys.stderr)
    sys.exit(1)

api = HfApi()
entries = api.list_repo_tree(repo_id=HF_REPO, path_in_repo=FOLDER, recursive=False)

files = []
for e in entries:
    if e.type != "file":
        continue
    cdn_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{FOLDER}/{e.path}"
    files.append({
        "path": e.path,
        "size": getattr(e, "size", None),
        "cdn_url": cdn_url,
        "added_at": datetime.utcnow().isoformat() + "Z"
    })

manifest = {
    "repo": HF_REPO,
    "folder": FOLDER,
    "generated_at": datetime.utcnow().isoformat() + "Z",
    "files": files
}

json.dump(manifest, sys.stdout, indent=2)
```

---

### 2) CDN-only iterable dataset (unchanged, robust)
No HF API, no `load_dataset`. Supports multi-worker sharding, retries, and skips corrupt files.

```python
# surrogate/data/cdn_dataset.py
import json
import io
import gzip
from typing import Dict, Any, Iterator, Optional
import torch
from torch.utils.data import IterableDataset
import requests
from surrogate.parsers import project_to_prompt_response  # per-format projector

class CdnDataset(IterableDataset):
    """
    Streams files directly from CDN URLs listed in a manifest.
    Each item is projected to {prompt, response} at parse time.
    """
    def __init__(self, manifest_path: str, max_retries: int = 3, timeout: float = 30.0):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.file_infos = manifest["files"]
        self.max_retries = max_retries
        self.timeout = timeout

    def _iter_urls(self) -> Iterator[Dict[str, Any]]:
        for fi in self.file_infos:
            yield fi

    def _fetch(self, url: str) -> bytes:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, timeout=self.timeout)
                resp.raise_for_status()
                return resp.content
            except Exception as exc:
                if attempt == self.max_retries:
                    raise RuntimeError(f"Failed to fetch {url} after {attempt} attempts") from exc
        raise RuntimeError(f"Failed to fetch {url}")

    def _project_bytes(self, content: bytes, path: str) -> Dict[str, str]:
        if path.endswith(".gz"):
            content = gzip.decompress(content)
        return project_to_prompt_response(content, path)

    def __iter__(self) -> Iterator[Dict[str, str]]:
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            iter_slice = self._iter_urls()
        else:
            per_worker = len(self.file_infos) // worker_info.num_workers
            wid = worker_info.id
            start = wid * per_worker
            end = start + per_worker if wid < worker_info.num_workers - 1 else len(self.file_infos)
            iter_slice = (self.file_infos[i] for i in range(start, end))

        for fi in iter_slice:
            try:
                raw = self._fetch(fi["cdn_url"])
                item = self._project_bytes(raw, fi["path"])
                if "prompt" not in item or "response" not in item:
                    continue
                yield item
            except Exception as exc:
                # log and skip bad files to keep stream alive
                print(f"Skipping {fi['cdn_url']}: {exc}")
                continue
```

---

### 3) Parser with parquet fast-path (unified)
Keep the generic JSON/JSONL fallback; add optional pyarrow parquet support for speed and schema correctness.

```python
# surrogate/parsers/__init__.py
import json

try:
    import pyarrow.parquet as pq
    import pyarrow as pa
    _PARQUET_AVAILABLE = True
except Exception:
    _PARQUET_AVAILABLE = False


def project_to_prompt_response(content: bytes, path: str) -> dict:
    """
    Project raw file bytes to {prompt, response}.
    Extend with format-specific logic (jsonl, json, parquet via pyarrow, etc).
    """
    # Parquet fast-path (avoids per-row Python overhead when available)
    if _PARQUET_AVAILABLE and path.endswith(".parquet"):
        try:
            table = pq.read_table(io.BytesIO(content))
            # Prefer common field names; take first valid row
            for col in ("prompt", "user", "input", "question"):
                if col in table.column_names:
                    prompts = table[col].to_pylist()
                    break
            else:
                prompts = []
            for col in ("response", "assistant", "output", "answer"):
                if col in table.column_names:
                    responses = table[col].to_pylist()
                    break
            else:
                responses = []

            # Yield first valid pair if possible; fallback to empty
            for p, r in zip(prompts, responses):
                if p is not None and r is not None and str(p).strip() and str(r).strip():
                    return {"prompt": str(p), "response": str(r)}
            return {"prompt": "", "response": ""}
        except Exception:
            # fall through to text handling below
            pass

    # Text-based formats
    text = content.decode("utf-8").strip()
    if path.endswith(".jsonl"):
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            prompt = obj.get("prompt") or obj.get("user") or obj.get("input") or ""
            response = obj.get("response") or obj.get("assistant") or obj.get("output") or ""
            if prompt and response:
                return {"prompt": str(prompt), "response": str(response)}
    elif path.endswith(".json"):
        obj = json.loads(text)
        prompt = obj.get("prompt") or obj.get("user") or ""
        response = obj.get("response") or obj.get("assistant") or ""
        if prompt and response:
            return {"prompt": str(prompt), "response": str(response)}
    else:
        # fallback: treat whole file as prompt, empty response (customize as needed)
        return {"prompt": text, "response": ""}

    return {"prompt": "", "response": ""}
```

---

### 4) Training entrypoint usage (unchanged)

```python
# surrogate/train.py (excerpt)
from torch.utils.data import DataLoader
from surrogate.data.cdn_dataset import CdnDataset

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to file_manifest.json")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()


