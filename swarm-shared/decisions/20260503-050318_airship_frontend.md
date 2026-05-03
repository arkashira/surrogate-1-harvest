# airship / frontend

## Final Synthesized Implementation (Best of Both Candidates)

**Goal**: Embed a CDN-only file manifest into Surrogate-1 training so Lightning Studio runs with **zero HF API calls during data loading**, eliminating 429s and quota burn.

**Why this wins**:
- Pure orchestration change (no model code touched)
- Single manifest + streaming loader (fast, safe)
- CDN URLs require no auth and bypass HF rate limits
- Fallback to local cache prevents training stalls
- Reuses running Studio to avoid quota waste

---

## 90-Minute Execution Plan

| Step | Time | Action |
|------|------|--------|
| 1 | 10m | Locate dataset loader in training script |
| 2 | 15m | Add `tools/build_cdn_manifest.py` (single `list_repo_tree` → `train_manifest.json`) |
| 3 | 10m | Generate manifest on Mac before push (or in CI after rate-limit window) |
| 4 | 20m | Add `surrogate/data/cdn_stream.py` (streaming parquet loader, projects `{prompt,response}`) |
| 5 | 15m | Wire into training entrypoint with `--manifest` flag and local-cache fallback |
| 6 | 10m | Update Lightning launcher to reuse running Studio and copy manifest into workspace |
| 7 | 10m | Smoke test 100 rows locally; verify zero HF API traffic |

---

## Code (Production-Ready)

### 1. Manifest generator (run on Mac or CI)

```python
# tools/build_cdn_manifest.py
#!/usr/bin/env python3
"""
Generate CDN-only file manifest for Surrogate-1 training.
Run after HF API window clears.
"""
import argparse
import json
import os
from datetime import datetime, timezone
from huggingface_hub import HfApi

API_TOKEN = os.getenv("HF_TOKEN")
DEFAULT_REPO = "axentx/surrogate-1-dataset"

def build_manifest(repo_id: str, date_folder: str, out_path: str):
    api = HfApi(token=API_TOKEN)
    files = api.list_repo_tree(
        repo_id=repo_id,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": [
            {
                "path": f.rfilename,
                "cdn_url": (
                    f"https://huggingface.co/datasets/{repo_id}"
                    f"/resolve/main/{f.rfilename}"
                ),
            }
            for f in files
            if f.rfilename.endswith(".parquet")
        ],
    }

    with open(out_path, "w") as fp:
        json.dump(manifest, fp, indent=2)

    print(f"Wrote {len(manifest['files'])} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--date-folder", required=True)
    parser.add_argument("--out", default="train_manifest.json")
    args = parser.parse_args()

    build_manifest(args.repo, args.date_folder, args.out)
```

Make executable:

```bash
chmod +x tools/build_cdn_manifest.py
```

---

### 2. CDN streaming dataset loader

```python
# surrogate/data/cdn_stream.py
import json
import pyarrow.parquet as pq
import requests
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional

MANIFEST_NAME = "train_manifest.json"

def load_manifest(manifest_path: str = MANIFEST_NAME) -> Dict:
    with open(manifest_path) as f:
        return json.load(f)

def stream_parquet_from_cdn(cdn_url: str, timeout: int = 30) -> Optional[pq.Table]:
    resp = requests.get(cdn_url, stream=True, timeout=timeout)
    resp.raise_for_status()
    buf = BytesIO(resp.content)
    return pq.read_table(buf, columns=["prompt", "response"])

class CdnDataset:
    def __init__(self, manifest_path: str = MANIFEST_NAME, cache_dir: str = "cache"):
        self.manifest = load_manifest(manifest_path)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

    def __iter__(self):
        for item in self.manifest["files"]:
            path = item["path"]
            try:
                table = stream_parquet_from_cdn(item["cdn_url"])
                for row in table.to_pylist():
                    prompt = row.get("prompt", "")
                    response = row.get("response", "")
                    if prompt or response:
                        yield {"prompt": prompt, "response": response}
            except Exception as exc:
                # Fallback to local cache if available
                cache_path = self.cache_dir / path
                if cache_path.is_file():
                    try:
                        table = pq.read_table(cache_path, columns=["prompt", "response"])
                        for row in table.to_pylist():
                            yield {"prompt": row.get("prompt", ""), "response": row.get("response", "")}
                    except Exception:
                        pass
                print(f"Skipped {path}: {exc}")
                continue
```

---

### 3. Training entrypoint with fallback

```python
# surrogate/train.py
import argparse
from pathlib import Path
from surrogate.data.cdn_stream import CdnDataset

try:
    from datasets import load_dataset
except ImportError:
    load_dataset = None

def load_train_data(manifest_path: str = "train_manifest.json", use_cdn: bool = True):
    if use_cdn and Path(manifest_path).is_file():
        print("Using CDN dataset (zero HF API calls)")
        return list(CdnDataset(manifest_path))

    if load_dataset is None:
        raise RuntimeError("No dataset source available")

    print("Falling back to HF datasets (may incur API calls)")
    ds = load_dataset("axentx/surrogate-1-dataset", split="train")
    return [{"prompt": row["prompt"], "response": row["response"]} for row in ds]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", action="store_true", default=True)
    parser.add_argument("--no-manifest", dest="manifest", action="store_false")
    args = parser.parse_args()

    train_data = load_train_data(use_cdn=args.manifest)
    print(f"Loaded {len(train_data)} rows")
    # ... continue with tokenization / training loop

if __name__ == "__main__":
    main()
```

---

### 4. Lightning Studio launcher (reuse + manifest)

```python
# scripts/launch_lightning_studio.py
import shutil
from pathlib import Path
from lightning_sdk import Teamspace, Studio, Machine

MANIFEST_SRC = Path("train_manifest.json")
STUDIO_NAME = "surrogate-train"

def get_or_create_studio():
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == STUDIO_NAME and s.status == "Running":
            print(f"Reusing running studio: {STUDIO_NAME}")
            return s
    print(f"Creating studio: {STUDIO_NAME}")
    return Studio.create(name=STUDIO_NAME, machine=Machine.L40S, container="pytorch/pytorch:latest")

def launch():
    studio = get_or_create_studio()

    # Copy manifest into workspace so training uses CDN
    if MANIFEST_SRC.is_file():
        target = Path(studio.workspace_path) / MANIFEST_SRC.name
        shutil.copy(MANIFEST_SRC, target)
        print(f"Copied manifest to studio workspace: {target}")

    # Run training (zero HF API calls during data loading)
    studio.run(
        command="python surrogate/train.py --manifest",
        sync=True,
    )

if __name__ == "__main__":
    launch()
```

---

## Validation Checklist

- [ ] `tools/build_cdn_manifest.py` runs and produces valid `train_manifest.json`
- [ ] CDN URLs in manifest are publicly accessible (no `Authorization` required)
- [ ] `CdnDataset` streams and projects `{prompt, response}` correctly
- [ ] Training
