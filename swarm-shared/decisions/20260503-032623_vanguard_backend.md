# vanguard / backend

## Final synthesized solution (correctness + concrete actionability)

**Core problem**: training jobs hit HF API at runtime (429s, non-reproducible runs) and lack a pinned, CDN-first snapshot.  
**Goal**: one deterministic manifest per date/slug; training must use only CDN URLs and never call HF APIs during data loading; Lightning Studio lifecycle must be safe (reuse + restart).

---

### 1) Manifest builder (single source of truth)

File: `/opt/axentx/vanguard/backend/manifest.py`

```python
#!/usr/bin/env python3
"""
CDN-first, content-addressed manifest for HF datasets.
Produces manifest-{date}-{digest}.json keyed by date/slug.
"""
import json, hashlib, os, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

API = HfApi()

def list_date_files(repo_id: str, date_folder: str) -> List[Dict]:
    """
    Single API call to list files in date folder (non-recursive).
    Returns list of dicts with cdn_url, path, size, etag (when available).
    """
    tree = API.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
    files = [t for t in tree if t.type == "file"]
    out = []
    for f in files:
        cdn_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{f.path}"
        out.append({
            "path": f.path,
            "cdn_url": cdn_url,
            "size": f.size or 0,
            "etag": getattr(f, "etag", None),
        })
    return out

def build_manifest(repo_id: str, date_folder: str, out_dir: str = ".") -> str:
    """
    Build and save manifest JSON.
    Returns path to manifest.
    """
    files = list_date_files(repo_id, date_folder)
    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    # Content-address manifest by canonical file list
    file_list_str = json.dumps(
        [f["path"] for f in sorted(files, key=lambda x: x["path"])],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(file_list_str.encode()).hexdigest()[:16]
    manifest_name = f"manifest-{date_folder.replace('/', '-')}-{digest}.json"
    out_path = Path(out_dir) / manifest_name
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path}")
    return str(out_path)

if __name__ == "__main__":
    # CLI: python manifest.py <repo_id> <date_folder> [out_dir]
    if len(sys.argv) < 3:
        print("Usage: python manifest.py <repo_id> <date_folder> [out_dir]")
        sys.exit(1)
    repo_id, date_folder = sys.argv[1], sys.argv[2]
    out_dir = sys.argv[3] if len(sys.argv) > 3 else "."
    build_manifest(repo_id, date_folder, out_dir)
```

Key correctness choices:
- Single `list_repo_tree` call at manifest-build time only.
- Manifest is content-addressed (hash of sorted file list) → reproducible and cache-friendly.
- Stores CDN URLs; training must use these only.

---

### 2) CDN-only training loader (schema-safe)

File: `/opt/axentx/vanguard/backend/train.py`

```python
#!/usr/bin/env python3
"""
CDN-only training loader pinned by manifest.
No HF API calls during data loading.
"""
import json, sys, os, time
from pathlib import Path
from typing import Iterator, Tuple, Optional

import torch
from torch.utils.data import IterableDataset, DataLoader
import requests

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("Install: pip install pyarrow")
    sys.exit(1)

class CDNParquetIterable(IterableDataset):
    """
    Stream parquet files from CDN URLs listed in manifest.
    Projects only known-safe columns to avoid mixed-schema errors.
    """
    def __init__(
        self,
        manifest_path: str,
        columns: Tuple[str, ...] = ("prompt", "response"),
        max_retries: int = 3,
        backoff: float = 2.0,
    ):
        super().__init__()
        self.manifest = json.loads(Path(manifest_path).read_text())
        self.urls = [
            f["cdn_url"] for f in self.manifest["files"]
            if f["cdn_url"].endswith(".parquet")
        ]
        self.columns = columns
        self.max_retries = max_retries
        self.backoff = backoff

    def _fetch_table(self, url: str) -> Optional[pa.Table]:
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                return pq.read_table(pa.BufferReader(resp.content), columns=self.columns)
            except Exception as exc:
                wait = self.backoff ** attempt
                print(f"[{attempt}/{self.max_retries}] Failed {url}: {exc}. Retry in {wait:.1f}s")
                time.sleep(wait)
        print(f"Permanently skipping {url} after {self.max_retries} attempts")
        return None

    def __iter__(self) -> Iterator[Tuple[str, str]]:
        for url in self.urls:
            table = self._fetch_table(url)
            if table is None:
                continue
            try:
                df = table.to_pandas()
            except Exception as exc:
                print(f"Failed to convert {url} to pandas: {exc}")
                continue

            for _, row in df.iterrows():
                prompt = str(row.get("prompt", ""))
                response = str(row.get("response", ""))
                if prompt.strip() and response.strip():
                    yield prompt, response

def make_dataloader(
    manifest_path: str,
    batch_size: int = 8,
    num_workers: int = 0,
) -> DataLoader:
    dataset = CDNParquetIterable(manifest_path)
    return DataLoader(
        dataset,
        batch_size=batch_size,
        num_workers=num_workers,
    )

# ---- Lightning Studio lifecycle guard ----
def ensure_studio(name: str, machine: str = "L40S"):
    """
    Reuse running studio or start new one.
    Returns studio object or None if Lightning unavailable.
    """
    try:
        from lightning import Studio, Teamspace
    except ImportError:
        print("Lightning not available; skipping studio management.")
        return None

    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s

    print(f"Starting studio: {name}")
    return Studio(name=name, machine=machine, open=True, create_ok=True)

def run_training_on_studio(
    manifest_path: str,
    script_path: str,
    studio_name: str = "vanguard-train",
    machine: str = "L40S",
):
    """
    Run training script on Lightning Studio with manifest pinned.
    Restarts studio if stopped.
    """
    studio = ensure_studio(studio_name, machine=machine)
    if studio is None:
        print("Skipping studio launch (Lightning unavailable).")
        return

    if studio.status != "Running":
        print("Studio stopped; restarting...")
        studio.start(machine=machine)

    studio.run(
        ["python", script_path, "--manifest", manifest_path],
        cwd="/workspace",
    )

# ---- CLI ----
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser
