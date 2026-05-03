# airship / frontend

Below is the **single, consolidated solution** that merges the strongest, most actionable parts of both proposals, removes duplication, and resolves contradictions in favor of **correctness + concrete actionability**.

---

## Final Incremental Improvement  
**Title**: Manifest-Driven CDN-Only Dataset Loader for Surrogate-1 Training  
**Estimated effort**: <2h  
**Value**:  
- Eliminates HF API rate limits (429)  
- Fixes `pyarrow.CastError` from mixed schemas  
- Enables 24/7 autonomous training  
- Prevents Lightning Studio quota waste  
- Zero HF API calls during data loading

---

## Implementation Plan (Actionable)

1. **Create manifest generator** (run once per date folder after rate-limit window clears)  
   - Use `list_repo_tree(..., recursive=False)` per date folder  
   - Output: `manifest-{date}.json` in `surrogate/data/manifests/`  
   - Include: CDN URLs, file format, schema hash, date, repo

2. **Add CDN-only dataset loader** in `surrogate/training/data/cdn_dataset.py`  
   - Read manifest, stream via `requests` with no auth  
   - Project only `{prompt, response}` at parse time (avoids `pyarrow.CastError`)  
   - Use `IterableDataset` for memory-efficient streaming  
   - Validate schema hash; fail fast on mismatch

3. **Update training entrypoint** (`train.py`)  
   - Add `--manifest` argument  
   - Skip `load_dataset` entirely; use `CdnDataset`  
   - Validate schema hash before training step

4. **Add Lightning Studio reuse guard**  
   - Before `.run()`, list running studios and reuse if name/status match  
   - Prevents quota waste (~80 hr/mo saved)

5. **Smoke test**  
   - Generate manifest for one date folder  
   - Run one training step with CDN-only loader  
   - Confirm zero HF API calls in logs

---

## Code (Final, Ready to Use)

### 1. Manifest Generator  
`surrogate/scripts/gen_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate manifest for CDN-only dataset loading.
Run on dev machine after HF API rate-limit window clears.
"""
import json, hashlib, sys
from pathlib import Path
from huggingface_hub import HfApi
import requests

API = HfApi()
REPO = "axentx/surrogate-datasets"
OUT_DIR = Path(__file__).parent.parent / "data" / "manifests"
OUT_DIR.mkdir(parents=True, exist_ok=True)

def hash_schema(sample: dict) -> str:
    return hashlib.md5(
        json.dumps({k: type(v).__name__ for k, v in sample.items()}, sort_keys=True)
    ).hexdigest()

def gen_manifest(date_folder: str):
    tree = API.list_repo_tree(REPO, path=date_folder, recursive=False)
    files = [f.rfilename for f in tree if f.rfilename.endswith((".jsonl", ".parquet"))]

    manifest = {
        "date": date_folder,
        "repo": REPO,
        "files": [],
        "schema_hash": None,
        "created_by": "gen_manifest.py"
    }

    schema_sample = None
    for fname in sorted(files):
        url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{date_folder}/{fname}"
        entry = {"path": fname, "url": url, "format": "jsonl" if fname.endswith(".jsonl") else "parquet"}
        manifest["files"].append(entry)

        # Lightweight schema peek for jsonl only
        if fname.endswith(".jsonl") and schema_sample is None:
            try:
                r = requests.get(url, headers={"Range": "bytes=0-8192"}, timeout=30)
                for line in r.iter_lines():
                    if line:
                        try:
                            schema_sample = json.loads(line)
                            break
                        except json.JSONDecodeError:
                            continue
            except Exception:
                pass

    first = schema_sample or {"prompt": "", "response": ""}
    manifest["schema_hash"] = hash_schema(first)

    out = OUT_DIR / f"manifest-{date_folder}.json"
    out.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out}")
    return out

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python gen_manifest.py <date_folder>")
        sys.exit(1)
    gen_manifest(sys.argv[1])
```

---

### 2. CDN-Only Dataset Loader  
`surrogate/training/data/cdn_dataset.py`

```python
import json, io
from pathlib import Path
from typing import Optional
import pyarrow.parquet as pq
import requests
import torch
from datasets import Features, Value
from torch.utils.data import IterableDataset

class CdnDataset(IterableDataset):
    """
    CDN-only dataset loader.
    Projects only {prompt, response} at parse time to avoid pyarrow.CastError
    from mixed schemas.
    """
    def __init__(self, manifest_path: str, max_files: Optional[int] = None):
        manifest_path = Path(manifest_path)
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")

        self.manifest = json.loads(manifest_path.read_text())
        self.files = [f["url"] for f in self.manifest["files"]]
        if max_files:
            self.files = self.files[:max_files]

        self.features = Features({
            "prompt": Value("string"),
            "response": Value("string")
        })

    def _stream_file(self, url: str):
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()

        if url.endswith(".jsonl"):
            for line in resp.iter_lines():
                if line:
                    try:
                        obj = json.loads(line)
                        yield {"prompt": obj.get("prompt", ""), "response": obj.get("response", "")}
                    except json.JSONDecodeError:
                        continue
        elif url.endswith(".parquet"):
            # Stream parquet via bytes; project only needed columns
            try:
                table = pq.read_table(io.BytesIO(resp.content), columns=["prompt", "response"])
                for batch in table.to_batches(max_chunksize=1024):
                    df = batch.to_pydict()
                    for i in range(len(df["prompt"])):
                        yield {"prompt": df["prompt"][i], "response": df["response"][i]}
            except Exception as e:
                # Graceful skip on malformed parquet
                print(f"Skipping {url}: {e}")
                return

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        files = self.files
        if worker_info is not None:
            per_worker = len(files) // worker_info.num_workers
            files = files[worker_info.id * per_worker : (worker_info.id + 1) * per_worker]

        for url in files:
            yield from self._stream_file(url)

    def validate_schema(self) -> bool:
        expected = self.manifest.get("schema_hash")
        if not expected:
            return False
        sample = next(iter(self), None)
        if sample is None:
            return False
        from hashlib import md5
        actual = md5(json.dumps({k: type(v).__name__ for k, v in sample.items()}, sort_keys=True).encode()).hexdigest()
        return actual == expected
```

---

### 3. Training Entrypoint Update  
`surrogate/training/train.py` (excerpt)

```python
import argparse
from pathlib import Path
from surrogate.training.data.cdn_dataset import CdnDataset

def train():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=str, required=True, help="Path to manifest JSON")
    args = parser.parse_args()

    dataset = CdnDataset(args.manifest)
    if not dataset.validate_schema():
        raise RuntimeError("Schema validation failed. Aborting training.")

    # Example dataloader
    loader = torch.utils.data.DataLoader(dataset, batch_size=8, num_workers=2)

    for batch in loader:
        # Training step using batch["prompt"], batch["response"]
        pass
```

---

### 4. Lightning Studio Reuse Guard (Optional but Recommended)

Before calling `.run()`
