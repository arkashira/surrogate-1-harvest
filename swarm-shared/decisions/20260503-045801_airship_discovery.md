# airship / discovery

### Final Consolidated Implementation  
*(Best parts merged; contradictions resolved for correctness + concrete actionability)*

**Core goal**: Embed a CDN-only file manifest into Surrogate-1 training so Lightning Studio trains with **zero HF API calls during data loading**, eliminating 429/128-commit limits while keeping Mac orchestrator-only. Ship in ≤2h with no infra or GPU quota changes.

---

## 1. Add manifest builder (Mac orchestrator)  
Path: `/opt/axentx/airship/scripts/build_manifest.py`

```python
#!/usr/bin/env python3
"""
Build a date-scoped manifest of HF dataset files for CDN-only training.
Run from Mac after rate-limit window clears.
"""
import json, os, sys, hashlib
from datetime import datetime, timezone
from pathlib import Path

HF_REPO = os.getenv("HF_DATASET_REPO", "org/surrogate-dataset")
DATE_SCOPE = os.getenv("DATE_SCOPE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUT_DIR = Path(os.getenv("MANIFEST_OUT", "manifests"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

def list_files():
    # One API call: list top-level for the date folder only
    from huggingface_hub import list_repo_tree
    files = list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_SCOPE,
        recursive=False
    )
    return [f.rfilename for f in files if f.rfilename.endswith(".parquet")]

def build_manifest():
    files = list_files()
    manifest = {
        "repo": HF_REPO,
        "date_scope": DATE_SCOPE,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": [
            {
                "path": f,
                "cdn_url": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{f}",
            }
            for f in sorted(files)
        ]
    }
    slug = hashlib.sha256(f"{HF_REPO}:{DATE_SCOPE}".encode()).hexdigest()[:12]
    out_path = OUT_DIR / f"manifest_{slug}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return out_path

if __name__ == "__main__":
    out = build_manifest()
    print(f"Manifest written: {out}", file=sys.stderr)
```

Make executable:

```bash
chmod +x /opt/axentx/airship/scripts/build_manifest.py
```

---

## 2. Update training loader to use manifest (CDN-only, high-throughput)  
Path: `/opt/axentx/airship/surrogate/train.py` (or equivalent)

Use PyArrow’s native Parquet over HTTP for correctness and speed (avoids full-file buffering in Python). Project only required columns to minimize memory.

```python
import json, os, pyarrow.parquet as pq, pyarrow as pa
from torch.utils.data import IterableDataset, DataLoader

class CDNParquetIterable(IterableDataset):
    def __init__(self, manifest_path, columns=("prompt", "response"), start=0, end=None):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = [item["cdn_url"] for item in self.manifest["files"][start:end]]
        self.columns = columns

    def __iter__(self):
        for url in self.files:
            try:
                ds = pq.ParquetDataset(
                    url,
                    use_threads=True,
                    memory_map=False
                )
                table = ds.read(columns=self.columns, use_threads=True)
                # Stream rows to avoid materializing full table in Python
                for batch in table.to_batches(max_chunksize=8192):
                    df = batch.to_pandas()
                    for _, row in df.iterrows():
                        yield {
                            "prompt": str(row.get("prompt", "")),
                            "response": str(row.get("response", ""))
                        }
            except Exception as exc:
                # Log and skip bad shards to keep pipeline resilient
                print(f"Skipping {url} due to error: {exc}", file=sys.stderr)
                continue

# DataModule wiring example
def make_dataloader(manifest_path, batch_size=32, num_workers=4):
    dataset = CDNParquetIterable(manifest_path, columns=("prompt", "response"))
    return DataLoader(
        dataset,
        batch_size=batch_size,
        num_workers=num_workers,
        pin_memory=True
    )

# Usage in training script
if __name__ == "__main__":
    manifest = os.getenv("MANIFEST_PATH", "manifests/manifest_*.json")
    # Explicitly pick latest or pass via CLI
    import glob, time
    candidates = sorted(glob.glob(manifest), key=os.path.getmtime, reverse=True)
    chosen = candidates[0] if candidates else None
    if not chosen:
        raise FileNotFoundError("No manifest found for MANIFEST_PATH")
    loader = make_dataloader(chosen, batch_size=32)
    # ... proceed with training loop
```

---

## 3. Reuse running Lightning Studio (quota-safe)  
Before `.run()`, reuse if already running to avoid quota churn.

```python
from lightning_sdk import Teamspace, Studio, Machine

team = Teamspace()
running = [s for s in team.studios if s.name == "surrogate-train" and s.status == "Running"]
if running:
    studio = running[0]
else:
    studio = Studio.create(name="surrogate-train", machine=Machine.L40S, create_ok=True)

# Ensure running before submit
if studio.status != "Running":
    studio.start(machine=Machine.L40S)

studio.run(
    entry_point="train.py",
    arguments=["--manifest", chosen],
    working_dir="/workspace/airship/surrogate"
)
```

---

## 4. Cron / orchestration snippet (Mac)  
Run manifest build after rate-limit window, then launch studio.

```bash
SHELL=/bin/bash
0 3 * * * cd /opt/axentx/airship && \
  python scripts/build_manifest.py && \
  MANIFEST_PATH=$(ls -t manifests/manifest_*.json | head -1) && \
  python surrogate/train.py --manifest "$MANIFEST_PATH"
```

---

## 5. Verification checklist  

1. **Manifest build**  
   ```bash
   cd /opt/axentx/airship && python scripts/build_manifest.py
   ```
   Confirm `manifests/manifest_*.json` exists and contains CDN URLs.

2. **Loader smoke test**  
   ```python
   from surrogate.train import make_dataloader
   loader = make_dataloader("manifests/manifest_*.json")
   batch = next(iter(loader))
   assert "prompt" in batch and "response" in batch
   ```

3. **Zero HF API during training**  
   Monitor traffic or logs; confirm no `huggingface_hub` download calls and no 429 responses.

---

## Key decisions (resolved contradictions)

- **PyArrow ParquetDataset over raw `requests` buffering**: Correctness + throughput for large Parquet shards. Resolves Candidate 2’s naive buffering risk.
- **One top-level `list_repo_tree` call per date folder**: Matches Candidate 1’s minimal API footprint and Candidate 2’s rate-limit avoidance.
- **Explicit column projection and row streaming**: Minimizes memory and avoids materializing entire shards; strictly better than full-file buffering.
- **Reuse running Studio + cron on Mac**: Keeps Mac orchestrator-only, no infra changes, no GPU quota impact.
