# vanguard / discovery

## Final consolidated solution (single, correct, actionable)

**Goal:** Eliminate HF API calls and 429s during training, guarantee parquet integrity, and make every training slice reproducible from a static manifest.

---

### 1. Architecture (one source of truth)
- **Manifest file** (committed to repo or stored with artifacts):  
  `manifest-{slug}.json`  
  schema:
  ```json
  {
    "repo": "axentx/surrogate-1",
    "slice": "batches/mirror-merged/2026-05-03",
    "generated_at_utc": "...",
    "entries": [
      {
        "path": "batches/mirror-merged/2026-05-03/part-00000.parquet",
        "size": 12345678,
        "sha256": "..."
      }
    ]
  }
  ```
- **Training** loads only via CDN URLs and verifies `sha256` + optional `size` before parsing.  
- **Lightning Studio reuse** checks running status before launch and restarts only if stopped (avoids quota waste).

---

### 2. Implementation files (single coherent set)

#### `/opt/axentx/vanguard/discovery/create_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest-{slug}.json with sha256 for every parquet in a slice.
Run from dev machine or CI after rate-limit window.
"""
import json, hashlib, io, os, sys, requests
from huggingface_hub import HfApi

REPO = "axentx/surrogate-1"
CDN_ROOT = "https://huggingface.co/datasets"

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def build_manifest(slice_path: str, out_path: str | None = None) -> str:
    api = HfApi()
    entries = api.list_repo_tree(repo_id=REPO, path=slice_path, recursive=True)

    files = []
    for e in entries:
        if not e.path.endswith(".parquet"):
            continue
        url = f"{CDN_ROOT}/{REPO}/resolve/main/{e.path}"
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        data = resp.content
        files.append({
            "path": e.path,
            "size": len(data),
            "sha256": sha256_bytes(data),
        })

    manifest = {
        "repo": REPO,
        "slice": slice_path.rstrip("/"),
        "generated_at_utc": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "entries": files,
    }

    out = out_path or f"manifest-{os.path.basename(slice_path.rstrip('/'))}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"Wrote {out} with {len(files)} parquet files")
    return out

if __name__ == "__main__":
    slice_path = sys.argv[1] if len(sys.argv) > 1 else "batches/mirror-merged/2026-05-03"
    build_manifest(slice_path)
```

---

#### `/opt/axentx/vanguard/discovery/train_cdn_only.py`
```python
#!/usr/bin/env python3
"""
Lightning-compatible training entrypoint that uses CDN-only fetches
and verifies integrity before parsing.
"""
import json, hashlib, io, os, sys, requests
from typing import Iterator
import pyarrow.parquet as pq
import torch
from torch.utils.data import IterableDataset, DataLoader

CDN_ROOT = "https://huggingface.co/datasets"

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path: str, max_items: int | None = None):
        with open(manifest_path, encoding="utf-8") as f:
            self.manifest = json.load(f)
        self.repo = self.manifest["repo"]
        self.entries = self.manifest["entries"]
        self.max_items = max_items

    def _download_and_verify(self, entry) -> bytes:
        url = f"{CDN_ROOT}/{self.repo}/resolve/main/{entry['path']}"
        resp = requests.get(url, stream=True, timeout=120)
        resp.raise_for_status()
        data = resp.content

        if entry.get("size") is not None and len(data) != entry["size"]:
            raise ValueError(f"Size mismatch: {entry['path']}")
        if entry.get("sha256") and sha256_bytes(data) != entry["sha256"]:
            raise ValueError(f"SHA256 mismatch: {entry['path']}")
        return data

    def __iter__(self) -> Iterator[dict]:
        count = 0
        for entry in self.entries:
            if self.max_items is not None and count >= self.max_items:
                break
            data = self._download_and_verify(entry)
            table = pq.read_table(io.BytesIO(data), columns=["prompt", "response"])
            df = table.to_pandas()
            for _, row in df.iterrows():
                yield {
                    "prompt": str(row["prompt"]),
                    "response": str(row["response"]),
                }
                count += 1
                if self.max_items is not None and count >= self.max_items:
                    break

# Replace with real surrogate-1 tokenizer
def tokenize(batch: dict) -> dict:
    # Minimal stub: returns fixed-shape dummy tensors
    n = len(batch["prompt"])
    return {
        "input_ids": torch.zeros(n, 128, dtype=torch.long),
        "labels": torch.zeros(n, 128, dtype=torch.long),
    }

def main():
    manifest = sys.argv[1] if len(sys.argv) > 1 else "manifest-2026-05-03.json"
    dataset = CDNParquetDataset(manifest, max_items=50_000)
    loader = DataLoader(dataset, batch_size=8, num_workers=0)

    for batch in loader:
        tokens = tokenize(batch)
        # training step placeholder
        print(f"Batch: prompts={len(batch['prompt'])}, shapes={tokens['input_ids'].shape}")
        break
    print("CDN-only data load OK")

if __name__ == "__main__":
    main()
```

---

#### `/opt/axentx/vanguard/discovery/lightning_studio_reuse.py`
```python
#!/usr/bin/env python3
"""
Reuse a running Lightning Studio or start one (L40S fallback).
Checks status before run to avoid idle-timeout waste.
"""
import os, sys, subprocess

try:
    from lightning.pytorch.studio import Studio, Teamspace
    STUDIO_AVAILABLE = True
except Exception:
    STUDIO_AVAILABLE = False
    Studio = Teamspace = None

def get_or_create_studio(name: str = "vanguard-train", preferred_machine: str = "L40S"):
    if not STUDIO_AVAILABLE:
        print("Lightning Studio SDK unavailable; skipping studio reuse.")
        return None

    ts = Teamspace()
    for s in ts.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s

    machines = [preferred_machine, "L40S", "A100", "A10G"]
    for m in machines:
        try:
            print(f"Creating studio '{name}' on {m}")
            return Studio(create_ok=True, name=name, machine=m)
        except Exception as e:
            print(f"Failed on {m}: {e}")
            continue
    raise RuntimeError("Could not create studio on any machine")

def run_with_studio_guard(train_script: str, studio_name: str = "vanguard-train"):
    studio = get_or_create_studio(name=studio_name)
    if studio is None:
        # Fallback: run locally (dev only)
        subprocess.run([sys.executable, train_script], check=True)
        return

    if
