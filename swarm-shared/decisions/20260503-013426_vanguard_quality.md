# vanguard / quality

Below is the **single, consolidated implementation** that merges the strongest, most actionable parts of both proposals, removes duplication, and resolves contradictions in favor of **correctness + concrete actionability**.

---

## 1. Diagnosis (merged)

- **Authenticated `list_repo_tree` on every load** burns HF quota (1000/5 min) and causes 429s.
- **No persisted `(repo, dateFolder) → file-list` manifest**; sessions re-enumerate via API instead of using a cached JSON.
- **Training uses API-backed enumeration or `load_dataset`**, risking mixed-schema errors (`pyarrow.CastError`) and extra API calls.
- **No CDN-only fetch path**; authenticated API calls remain the default during data loading.
- **Missing reuse of Lightning Studio**; jobs recreate and waste quota.

---

## 2. Proposed change (merged)

Add a **manifest-driven, CDN-only training path** and **reuse existing Studio**:

1. **`build_manifest.py`** — run once per `dateFolder` to produce a persisted JSON manifest.
2. **`train.py`** — replace enumeration/`load_dataset` with a **CDN-only `IterableDataset`** that streams Parquet via public CDN URLs.
3. **`launch_studio.py`** — patch to reuse a running Studio instead of recreating.

Scope: ~120 LoC total.  
Impact: eliminates authenticated enumeration during training and enforces manifest-driven CDN fetches.

---

## 3. Implementation (final, merged)

### 3.1 Manifest builder (run once per `dateFolder`)

File: `/opt/axentx/vanguard/scripts/build_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate repo+dateFolder → file-list manifest for CDN-only training.
Run from Mac or CI after rate-limit window clears.
"""
import json
import os
import sys
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-data")
DATE_FOLDER = sys.argv[1] if len(sys.argv) > 1 else "2026-04-29"
OUT_DIR = Path(__file__).parent.parent / "manifests"
OUT_PATH = OUT_DIR / f"{DATE_FOLDER}.json"

def main() -> None:
    api = HfApi()
    # Single non-recursive call per dateFolder
    items = api.list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_FOLDER,
        repo_type="dataset",
        recursive=False,
    )
    files = sorted(it.rfilename for it in items if it.type == "file")
    manifest = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "files": files,
        "cdn_prefix": (
            f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{DATE_FOLDER}"
        ),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files -> {OUT_PATH}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

Usage:

```bash
HF_DATASET_REPO=your-org/your-dataset ./build_manifest.py 2026-04-29
```

---

### 3.2 CDN-only data loader in training script

File: `/opt/axentx/vanguard/train/train.py` (patch region)

```python
import json
import os
import requests
import pyarrow as pa
import pyarrow.parquet as pq
from pathlib import Path
from torch.utils.data import IterableDataset, DataLoader

MANIFEST_PATH = Path(__file__).parent.parent / "manifests" / "2026-04-29.json"

class CDNParquetDataset(IterableDataset):
    """
    CDN-only Parquet streamer.
    - Uses public CDN URLs (no Authorization header) to bypass /api/ rate limits.
    - Projects to {prompt, response} to avoid mixed-schema errors.
    """

    def __init__(self, manifest_path: Path = MANIFEST_PATH):
        manifest_path = Path(manifest_path)
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest missing: {manifest_path}")
        self.manifest = json.loads(manifest_path.read_text())
        self.prefix = self.manifest["cdn_prefix"]
        self.files = [f for f in self.manifest["files"] if f.endswith(".parquet")]
        if not self.files:
            raise ValueError("No Parquet files found in manifest")

    def __iter__(self):
        for fname in self.files:
            url = f"{self.prefix}/{fname}"
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            table = pq.read_table(pa.BufferReader(resp.content))

            # Project to required schema; ignore extra/mixed columns
            required = {"prompt", "response"}
            present = {c for c in table.column_names if c in required}
            if present != required:
                continue

            batch = pa.table({c: table.column(c) for c in ("prompt", "response")})
            for i in range(batch.num_rows):
                yield {k: batch.column(k)[i].as_py() for k in ("prompt", "response")}

# In your LightningDataModule
def train_dataloader(self):
    dataset = CDNParquetDataset()
    return DataLoader(
        dataset,
        batch_size=self.batch_size,
        num_workers=4,
        pin_memory=True,
    )
```

---

### 3.3 Reuse running Lightning Studio (save quota)

File: `/opt/axentx/vanguard/scripts/launch_studio.py` (patch)

```python
from lightning import Studio
from huggingface_hub import get_running_studio

def launch_or_reuse_studio(
    name: str,
    repo: str,
    script: str,
    instance_type: str = "cpu-small",
    wait_for_ready: bool = True,
):
    """
    Reuse an existing running Studio if present; otherwise create one.
    Prevents duplicate jobs that waste quota.
    """
    running = get_running_studio(name=name, repo=repo)
    if running is not None:
        print(f"Reusing running Studio: {running.id}")
        if wait_for_ready:
            running.wait_until_ready()
        return running

    studio = Studio.create(
        name=name,
        repo=repo,
        script=script,
        instance_type=instance_type,
        wait_for_ready=wait_for_ready,
    )
    print(f"Created new Studio: {studio.id}")
    return studio
```

Usage in training launcher:

```python
launch_or_reuse_studio(
    name="vanguard-train",
    repo="datasets/your-data",
    script="train.py",
    instance_type="gpu-small",
)
```

---

## 4. Verification (merged)

1. Run manifest builder once:
   ```bash
   HF_DATASET_REPO=your-org/your-dataset ./build_manifest.py 2026-04-29
   ```
   Confirm `manifests/2026-04-29.json` exists and lists expected Parquet files.

2. Run training:
   ```bash
   python train.py
   ```
   - Confirm **no `list_repo_tree` calls** appear in logs or network traces.
   - Confirm downloads use `https://huggingface.co/datasets/...` CDN URLs (no Authorization header).

3. Monitor HF API quota:
   - Authenticated API calls should drop sharply.
   - 429 errors should disappear.

4. Validate training completes successfully and yields expected metrics.

5. Confirm Studio reuse behavior:
   - Second launch should print `Reusing running Studio` instead of creating a new one.

---

## 5. Notes & Best Practices

- **Security**: CDN URLs are public; ensure repo/files are intended for public access or use tokens only when strictly required.
- **Schema drift**: The projection to `{prompt, response}` avoids `pyarrow.CastError` from mixed schemas; extend if more columns are needed.
- **Retries**: Add exponential backoff for CDN downloads in production (omitted above for brevity).
- **C
