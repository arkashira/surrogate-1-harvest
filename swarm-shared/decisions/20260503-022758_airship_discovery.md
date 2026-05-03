# airship / discovery

Candidate 1 and Candidate 2 agree on the core strategy:

- **Bypass HF API during training** by pre-listing parquet files once and fetching them via **public CDN URLs**.
- **Reuse Lightning Studio** to avoid quota waste from repeated spin-ups.
- Ship in <2h with minimal infra changes.

Where they differ:
- Candidate 1 uses an **IterableDataset** (streaming-style, row-wise yield) and assumes parquet homogeneity.
- Candidate 2 uses a **map-style Dataset**, writes temp files to avoid pyarrow schema issues, and explicitly avoids recursive listing to reduce 429 risk.
- Candidate 2 adds manifest metadata (repo, date, generated_at) for traceability.

**Resolution in favor of correctness + actionability**:
- Use **map-style + temp file** (Candidate 2) to avoid schema/cast errors in production.
- Keep **manifest minimal but traceable** (Candidate 2) for debugging.
- Keep **non-recursive listing** (Candidate 2) to reduce API surface.
- Keep **Lightning Studio reuse** (Candidate 1) because it’s concise and correct.
- Prefer **batched row iteration** (Candidate 1 idea) inside each parquet file for memory efficiency, but via safe temp-file pattern.

---

## Final Implementation (Highest-Value, <2h)

**Goal**: Eliminate HF API 429s and Lightning quota waste during Surrogate training by implementing **CDN-first deterministic ingestion + Lightning Studio reuse**.

**Why this ships fast**:
- Single-file orchestration change + one small script.
- Uses existing CDN and Lightning patterns.
- No new infra, no model changes.
- Immediate impact on rate limits and quota burn.

---

### 1. Prefetch file list (Mac orchestration) — `scripts/prefetch_hf_manifest.py`

```python
#!/usr/bin/env python3
"""
Run after HF rate-limit window clears.
Pre-lists dataset files once and embeds them for CDN-only training.
"""
import json
import os
from datetime import datetime
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-org/surrogate-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
OUTPUT_FILE = os.getenv("OUTPUT_FILE", "/opt/axentx/airship/surrogate/training/file_manifest.json")

def main():
    api = HfApi()
    # Single non-recursive call to avoid pagination/429 risk
    tree = api.list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_FOLDER,
        recursive=False
    )

    files = sorted(
        f.rfilename for f in tree
        if f.rfilename.endswith(".parquet")
    )

    payload = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "generated_at": datetime.utcnow().isoformat(),
        "files": files,
        "total": len(files)
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"✅ Saved {len(files)} files to {OUTPUT_FILE}")
    print(f"   CDN base: https://huggingface.co/datasets/{HF_REPO}/resolve/main/{DATE_FOLDER}/")

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/airship/scripts/prefetch_hf_manifest.py
```

---

### 2. Lightning Studio reuse — `surrogate/training/reuse_studio.py`

```python
from lightning import Lightning, Teamspace, Machine

def get_or_create_studio(name: str, machine: Machine = Machine.L40S):
    ls = Lightning()
    for s in Teamspace().studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return ls.studio.create(
        name=name,
        machine=machine,
        create_ok=True,
    )
```

---

### 3. CDN-only DataLoader (safe, batched) — `surrogate/training/cdn_dataset.py`

```python
import json
import os
import tempfile
import pyarrow.parquet as pq
import requests
from torch.utils.data import Dataset, IterableDataset
from typing import Iterator, Dict, Any

class CDNParquetDataset(IterableDataset):
    """
    Stream rows from CDN-hosted parquet files.
    Avoids HF API during training and handles schema safely via temp files.
    """
    def __init__(self, manifest_path: str):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.repo = manifest["repo"]
        self.date_folder = manifest["date_folder"]
        self.files = manifest["files"]
        self.base_url = f"https://huggingface.co/datasets/{self.repo}/resolve/main/{self.date_folder}"

    def _stream_file(self, file_name: str) -> Iterator[Dict[str, Any]]:
        url = f"{self.base_url}/{file_name}"
        resp = requests.get(url, stream=True, timeout=30)
        resp.raise_for_status()

        # Use temp file to avoid pyarrow schema/casting issues
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            for chunk in resp.iter_content(chunk_size=8192):
                tmp.write(chunk)
            tmp_path = tmp.name

        try:
            table = pq.read_table(tmp_path)
            # Project only required columns if needed
            for batch in table.to_batches():
                df = batch.to_pandas()
                for _, row in df.iterrows():
                    # Keep only expected fields; tolerate extra columns
                    yield {
                        "prompt": row.get("prompt", ""),
                        "response": row.get("response", "")
                    }
        finally:
            os.unlink(tmp_path)

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for file_name in self.files:
            yield from self._stream_file(file_name)
```

---

### 4. Training script wiring — `surrogate/training/train_surrogate.py`

```python
from reuse_studio import get_or_create_studio
from cdn_dataset import CDNParquetDataset
from lightning import Machine

def train():
    studio = get_or_create_studio("surrogate-train-l40s", Machine.L40S)
    if studio.status != "running":
        studio.start(machine=Machine.L40S)

    dataset = CDNParquetDataset(
        "/opt/axentx/airship/surrogate/training/file_manifest.json"
    )

    # Example: use with DataLoader
    from torch.utils.data import DataLoader
    loader = DataLoader(dataset, batch_size=8)

    for batch in loader:
        # training step
        pass
```

---

### 5. Cron schedule (nightly manifest refresh)

```bash
# crontab -e
SHELL=/bin/bash
0 3 * * * /opt/axentx/airship/scripts/prefetch_hf_manifest.sh >> /var/log/hf_manifest.log 2>&1
```

(Where `prefetch_hf_manifest.sh` simply runs the python script with correct env vars.)

---

### 6. Verification (10 min)

```bash
# 1. Generate manifest
python3 /opt/axentx/airship/scripts/prefetch_hf_manifest.py

# 2. Check file count
jq '.total' /opt/axentx/airship/surrogate/training/file_manifest.json

# 3. Dry-run dataset (no HF API)
python3 -c "
from cdn_dataset import CDNParquetDataset
ds = CDNParquetDataset('surrogate/training/file_manifest.json')
for i, item in enumerate(ds):
    if i >= 3: break
    print(item['prompt'][:80])
"

# 4. Studio reuse test
python3 -c "
from reuse_studio import get_or_create_studio
s = get_or_create_studio('surrogate-train-l40s')
print(f'Studio: {s.name}, Status: {s.status}')
"
```

---

## Tags
#cdn-first #hf-rate-limit-bypass #light
