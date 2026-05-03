# airship / discovery

Below is the **single, consolidated plan + code** that keeps the best parts of the proposal, removes contradictions, and maximizes correctness + actionability for today’s Surrogate-1 run.

---

## One-sentence summary
Replace HF `load_dataset(streaming=True)` with a **manifest-driven, CDN-only iterable loader** that is built once on your Mac and consumed in Lightning Studio with zero HF auth/API calls and strict `{prompt, response}` schema enforcement.

---

## Correctness choices (resolve contradictions)

1. **Manifest scope**  
   - Use **per-folder non-recursive `list_repo_tree`** (not recursive) to avoid pagination timeouts and 429s while building.  
   - Include **all candidate extensions** (`.jsonl`, `.parquet`, `.json`) and store `sha256`/`size` for change detection.

2. **Schema safety**  
   - **Project to `{prompt, response}` only** and drop everything else.  
   - Accept flexible input keys (`prompt/input/question`, `response/output/answer`) but require both present; skip malformed rows.

3. **CDN vs auth**  
   - Use **public CDN URLs** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) — no tokens, no API, no 429s from HF datasets API.  
   - Respect CDN politeness with **exponential backoff + jitter** and timeouts.

4. **Streaming/shuffle behavior**  
   - Deterministic **per-worker file shuffle** via seed + worker id (avoids duplicate/missing files across workers).  
   - Do **not** rely on global shuffle across epochs; instead reshuffle file list each epoch (cheap, avoids full-buffer).

5. **Lightning Studio integration**  
   - Reuse running Studio when available; if stopped, restart on `Machine.L40S`.  
   - Pass manifest path via **CLI arg** (`--manifest`) and upload manifest as run reference (or embed in repo).  
   - Keep collator/tokenizer/train loop **unchanged**.

---

## Implementation plan (≤2h)

1. **Generate manifest on Mac** (one-time, after rate-limit window)  
   - Run:  
     ```bash
     python scripts/build_file_manifest.py \
       --repo <repo> \
       --date <YYYY-MM-DD> \
       --out manifests/files-<date>.json
     ```
   - Produces `manifests/files-<date>.json` with repo, folder, and file entries.

2. **Add CDN-only iterable dataset**  
   - Create `surrogate/data/cdn_dataset.py` (see code below).  
   - Downloads via CDN, projects schema, retries safely.

3. **Update training script**  
   - Replace `load_dataset(streaming=True, ...)` with `CdnIterableDataset(manifest_path=...)`.  
   - Add CLI arg `--manifest` (and optional `--repo`).  
   - Keep tokenizer/colator/train loop unchanged.

4. **Lightning Studio**  
   - Check Studio status; if stopped, restart on `Machine.L40S`.  
   - Pass manifest via CLI arg or upload as run reference.  
   - Validate zero HF API calls and correct schema on first 100 steps.

5. **Validation**  
   - Local smoke test: 100 steps with small manifest slice.  
   - Confirm: no 429s, correct `{prompt, response}` projection, deterministic worker file assignment.

---

## Final consolidated code

### 1) Build manifest (Mac orchestration)
```python
# scripts/build_file_manifest.py
#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from huggingface_hub import HfApi

def build_manifest(repo_id: str, date_folder: str, out_path: Path):
    api = HfApi()
    entries = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
    files = []
    for e in entries:
        if e.type == "file" and e.path.endswith((".jsonl", ".parquet", ".json")):
            files.append({
                "path": e.path,
                "size": e.size,
                "sha256": e.lfs.get("sha256", None) if e.lfs else None,
            })
    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "files": files,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out)
```

### 2) CDN-only iterable dataset
```python
# surrogate/data/cdn_dataset.py
#!/usr/bin/env python3
import json
import logging
import random
import time
from typing import Dict, Iterator, Optional

import requests
import pyarrow as pa
import pyarrow.parquet as pq
import torch
from torch.utils.data import IterableDataset

logger = logging.getLogger(__name__)

class CdnIterableDataset(IterableDataset):
    def __init__(
        self,
        manifest_path: str,
        repo_id: Optional[str] = None,
        max_retries: int = 5,
        seed: int = 42,
    ):
        super().__init__()
        self.manifest = json.loads(open(manifest_path).read())
        self.repo_id = repo_id or self.manifest["repo_id"]
        self.files = [f["path"] for f in self.manifest["files"]]
        if not self.files:
            raise ValueError("No files found in manifest")
        self.max_retries = max_retries
        self.seed = seed
        self.base_url = f"https://huggingface.co/datasets/{self.repo_id}/resolve/main"

    def _download_file(self, path: str) -> bytes:
        url = f"{self.base_url}/{path}"
        for attempt in range(self.max_retries):
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                return resp.content
            except Exception as exc:
                wait = (2 ** attempt) + random.random()
                logger.warning(
                    "Download %s failed (attempt %s): %s — retry in %.1fs",
                    path,
                    attempt + 1,
                    exc,
                    wait,
                )
                time.sleep(wait)
        raise RuntimeError(f"Failed to download {path} after {self.max_retries} attempts")

    def _project_record(self, raw: Dict) -> Optional[Dict]:
        prompt = raw.get("prompt") or raw.get("input") or raw.get("question")
        response = raw.get("response") or raw.get("output") or raw.get("answer")
        if prompt is None or response is None:
            return None
        return {"prompt": str(prompt), "response": str(response)}

    def _iter_file(self, path: str) -> Iterator[Dict]:
        content = self._download_file(path)
        try:
            if path.endswith(".jsonl"):
                for line in content.decode("utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    proj = self._project_record(rec)
                    if proj:
                        yield proj

            elif path.endswith(".parquet"):
                table = pq.read_table(pa.BufferReader(content))
                df = table.to_pandas()
                for _, row in df.iterrows():
                    proj = self._project_record(row.to_dict())
                    if proj:
                        yield proj

            elif path.endswith(".json"):
                data = json.loads(content.decode("utf-8"))
                items = data if isinstance(data, list) else [data]
                for rec in items:
                    proj = self._project_record(rec)
                    if proj:
                        yield proj

            else:
                logger.warning("Unsupported file type: %s", path)
        except Exception as exc:
            logger.error("Error processing %s: %s", path, exc)

    def __iter__(self) -> Iterator[Dict]:
       
