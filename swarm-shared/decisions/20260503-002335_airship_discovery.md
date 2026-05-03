# airship / discovery

## Final synthesized plan (highest correctness + concrete actionability)

**Core decision**  
Replace any `load_dataset(streaming=True)` or recursive HF API usage with a **manifest-driven, CDN-only iterable loader** that is generated once (locally/cron) and used during training. This removes all HF API calls during training, eliminates 429 risk, and safely handles heterogeneous schemas by projecting only `{prompt, response}` at parse time.

**Why this wins**  
- Zero HF API calls during training → zero 429 risk.  
- Explicit file list avoids pagination/recursion surprises and works for empty/small repos today and at scale.  
- Projection at parse time enforces schema safety.  
- Fits in <2h: one small module + one-line training change + optional cron.  
- Compatible with Lightning Studio reuse and local dev.

---

## Implementation (concrete, ready to run)

### 1) Discovery + manifest exporter (run on Mac / cron)

```python
# tools/build_hf_cdn_manifest.py
#!/usr/bin/env python3
"""
Build a CDN-only file manifest for a HuggingFace dataset repo folder.

Usage:
  python tools/build_hf_cdn_manifest.py \
    --repo "org/surrogate-mirror" \
    --folder "batches/mirror-merged/2026-05-03" \
    --out "surrogate/data/file_manifest.json"

Notes:
- Uses non-recursive list_repo_tree to avoid pagination blowup.
- Can be scheduled after known HF rate-limit windows.
"""
import argparse
import json
import os
import time
from typing import Dict, List
from huggingface_hub import list_repo_tree

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, folder: str, out_path: str, recursive: bool = False) -> Dict:
    # Non-recursive by default; set recursive=True only if you accept pagination cost.
    entries = list_repo_tree(repo=repo, path=folder, recursive=recursive)
    paths = [e.path for e in entries if e.path.lower().endswith(".parquet")]

    # Build CDN URLs explicitly for clarity/validation
    files = [{"path": p, "url": CDN_TEMPLATE.format(repo=repo, path=p)} for p in sorted(paths)]

    manifest = {
        "repo": repo,
        "folder": folder,
        "date": os.path.basename(folder.rstrip("/")),
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "recursive": recursive,
        "files": files,
        "note": "CDN-only manifest; use 'url' during training. No HF API required."
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {out_path}")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build HF CDN manifest")
    parser.add_argument("--repo", required=True, help="HF dataset repo (org/name)")
    parser.add_argument("--folder", required=True, help="Folder path in repo")
    parser.add_argument("--out", default="surrogate/data/file_manifest.json", help="Output JSON path")
    parser.add_argument("--recursive", action="store_true", help="List recursively (use with care)")
    args = parser.parse_args()
    build_manifest(repo=args.repo, folder=args.folder, out_path=args.out, recursive=args.recursive)
```

Make executable and optional cron example:

```bash
chmod +x tools/build_hf_cdn_manifest.py

# Example cron (run after HF rate-limit window):
# 0 2 * * * cd /opt/axentx/airship && \
#   python3 tools/build_hf_cdn_manifest.py \
#     --repo "org/surrogate-mirror" \
#     --folder "batches/mirror-merged/$(date -d yesterday +%Y-%m-%d)" \
#     --out "surrogate/data/file_manifest.json"
```

---

### 2) CDN-only iterable dataset (zero HF API during training)

```python
# surrogate/data/hf_cdn_dataset.py
import json
import time
import requests
import pyarrow.parquet as pq
import io
from typing import Dict, Iterator
from torch.utils.data import IterableDataset

class HFCDNDataset(IterableDataset):
    """
    CDN-only iterable dataset for HuggingFace parquet files.
    No HuggingFace API calls during iteration.
    Projects each row to {prompt, response} at parse time.
    """

    def __init__(
        self,
        manifest_path: str,
        max_retries: int = 5,
        backoff_factor: float = 1.5,
        timeout: int = 30,
        max_files: int = None,
    ):
        super().__init__()
        self.manifest_path = manifest_path
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self.max_files = max_files
        self._manifest = None

    def _load_manifest(self) -> Dict:
        if self._manifest is None:
            with open(self.manifest_path) as f:
                self._manifest = json.load(f)
        return self._manifest

    def _download_parquet(self, url: str) -> bytes:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, timeout=self.timeout, stream=False)
                resp.raise_for_status()
                return resp.content
            except Exception as exc:
                wait = self.backoff_factor ** attempt
                print(f"[HFCDN] Retry {attempt}/{self.max_retries} in {wait:.1f}s for {url}: {exc}")
                time.sleep(wait)
        raise RuntimeError(f"[HFCDN] Failed to download after {self.max_retries} attempts: {url}")

    def _parse_and_project(self, data: bytes) -> Iterator[Dict[str, str]]:
        table = pq.read_table(io.BytesIO(data))
        df = table.to_pandas()
        # Projection at parse time: keep only prompt/response (safe for heterogeneous schemas)
        for _, row in df.iterrows():
            prompt = str(row.get("prompt", row.get("input", "")))
            response = str(row.get("response", row.get("output", "")))
            if prompt and response:
                yield {"prompt": prompt, "response": response}

    def __iter__(self) -> Iterator[Dict[str, str]]:
        manifest = self._load_manifest()
        files = manifest.get("files", [])
        if self.max_files is not None:
            files = files[:self.max_files]

        for entry in files:
            url = entry["url"]
            path = entry["path"]
            try:
                data = self._download_parquet(url)
                for sample in self._parse_and_project(data):
                    yield sample
            except Exception as exc:
                print(f"[HFCDN] Skipping {path}: {exc}")
                continue
```

---

### 3) Wire into training (minimal, safe change)

```python
# surrogate/train.py  (excerpt)
import os
from torch.utils.data import DataLoader
from surrogate.data.hf_cdn_dataset import HFCDNDataset
from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.utilities.model_helpers import LightningStudio

# Optional Studio reuse guard (avoid duplicate studios)
def get_or_create_studio():
    # List running studios and reuse if present
    running = LightningStudio.list()
    if running:
        print(f"Reusing existing studio: {running[0].id}")
        return running[0]
    return LightningStudio.create()

# Use CDN dataset instead of load_dataset(...)
manifest_path = os.getenv("HF_MANIFEST_PATH", "surrogate/data/file_manifest.json")
train_dataset = HFCDNDataset(
    manifest_path=manifest_path,
    max_retries=5,
    backoff_factor=1.5,
    timeout=30,
    max_files=None,  # set for quick local tests
)

train_loader = DataLoader(
    train_dataset,
    batch_size=8,
    num_workers=2,
    pin_memory=True,
)

# Example LightningModule and Trainer usage (unchanged
