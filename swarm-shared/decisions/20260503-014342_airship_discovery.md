# airship / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

I merged the strongest, non-redundant parts from both candidates and resolved contradictions in favor of **correctness** and **concrete actionability**.

---

## 1. Problem & Goal (Merged)

- **Problem**: Surrogate training is blocked by HF API 429s and Lightning idle-timeouts during long data-loading epochs.
- **Goal**: Eliminate HF API 429s by switching to a **CDN-only ingestion pipeline** with a **pre-listed file manifest** and **deterministic sibling-repo spread**, plus fix Lightning idle-timeout training failures.

**Why this ships fast**:  
- Uses existing patterns (HF CDN bypass, sibling repo spread, Lightning Studio reuse)  
- No new infra — only orchestration + small script changes  
- Immediate reduction in 429s and training interruptions  

---

## 2. Implementation Plan (≤2h)

| Step | Owner | Time |
|------|-------|------|
| 1. Generate file manifest (Mac orchestration) | Me | 15m |
| 2. Add `train.py` CDN-only loader + manifest | Me | 45m |
| 3. Add Lightning Studio reuse + idle-restart guard | Me | 30m |
| 4. Smoke test on L40S (free tier) | Me | 30m |

---

## 3. Step-by-Step Implementation

### 3.1 Generate file manifest (run once on Mac)

```bash
# /opt/axentx/airship/scripts/generate_manifest.py
#!/usr/bin/env python3
"""
Generate a date-scoped manifest for CDN-only training.
Run after rate-limit window clears; commit to repo so Lightning uses zero API.
"""
import json, os
from huggingface_hub import list_repo_tree

REPO = "axentx/surrogate-dataset"
DATE_FOLDER = "batches/mirror-merged/2026-05-03"   # latest stable date
OUT_PATH = "data/manifest_2026-05-03.json"

def main():
    files = list_repo_tree(REPO, path=DATE_FOLDER, recursive=False)
    paths = [f.rfilename for f in files if f.rfilename.endswith(".parquet")]
    manifest = {
        "repo": REPO,
        "date": DATE_FOLDER,
        "files": sorted(paths),
        "total": len(paths)
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(paths)} files to {OUT_PATH}")

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/airship/scripts/generate_manifest.py
python3 /opt/axentx/airship/scripts/generate_manifest.py
```

---

### 3.2 CDN-only loader in `train.py`

```python
# /opt/axentx/airship/surrogate/train.py
import json, os, time
import torch
from torch.utils.data import Dataset, DataLoader
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "data/manifest_2026-05-03.json")
CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/surrogate_cdn_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

class CDNParquetDataset(Dataset):
    def __init__(self, manifest_path):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.repo = self.manifest["repo"]
        self.files = self.manifest["files"]
        self.base_url = f"https://huggingface.co/datasets/{self.repo}/resolve/main"

    def _download(self, path):
        url = f"{self.base_url}/{path}"
        out = os.path.join(CACHE_DIR, path.replace("/", "_"))
        if os.path.exists(out):
            return out

        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                r = requests.get(url, stream=True, timeout=60)
                r.raise_for_status()
                with open(out, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                return out
            except Exception as e:
                wait = 2 ** attempt
                print(f"CDN download failed (attempt {attempt}/{max_retries}): {e}. Retrying in {wait}s...")
                time.sleep(wait)
        raise RuntimeError(f"Failed to download {url} after {max_retries} attempts.")

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        rel = self.files[idx]
        local = self._download(rel)
        # Project to {prompt, response} only; ignore mixed schema cols
        tbl = pq.read_table(local, columns=["prompt", "response"])
        df = tbl.to_pandas().dropna()
        # Simple random row for this demo; real code yields tokenized tensors
        row = df.iloc[torch.randint(0, len(df), (1,)).item()]
        return {
            "prompt": row["prompt"],
            "response": row["response"]
        }

def build_dataloader(manifest_path=MANIFEST_PATH, batch_size=4, num_workers=2):
    ds = CDNParquetDataset(manifest_path)
    return DataLoader(ds, batch_size=batch_size, num_workers=num_workers, shuffle=True)

# Example usage inside LightningModule
# loader = build_dataloader()
# for batch in loader:
#     ...
```

---

### 3.3 Lightning Studio reuse + idle-restart guard

```python
# /opt/axentx/airship/surrogate/lightning_launcher.py
#!/usr/bin/env python3
import time
from lightning_sdk import Teamspace, Studio, Machine

TEAMSPACE = "axentx"
STUDIO_NAME = "surrogate-train-l40s"
MACHINE = Machine.L40S

def get_or_create_studio():
    ts = Teamspace(TEAMSPACE)
    running = [s for s in ts.studios if s.name == STUDIO_NAME and s.status == "Running"]
    if running:
        print(f"Reusing running studio: {STUDIO_NAME}")
        return running[0]
    print(f"Creating studio: {STUDIO_NAME}")
    return Studio.create(
        name=STUDIO_NAME,
        teamspace=TEAMSPACE,
        machine=MACHINE,
        create_ok=True
    )

def run_training(script_path="train.py", args=None):
    studio = get_or_create_studio()
    args = args or []
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        if studio.status != "Running":
            print(f"Studio stopped (attempt {attempt}). Restarting...")
            studio.start(machine=MACHINE)
            time.sleep(30)  # allow boot
        try:
            run = studio.run(
                command=["python3", script_path] + args,
                environment="BASE",
                cwd="/workspace/airship/surrogate"
            )
            run.watch()
            if run.status == "success":
                print("Training completed.")
                return
            else:
                print(f"Run failed: {run.status}. Logs:")
                print(run.logs())
        except Exception as e:
            print(f"Run error: {e}")
        print(f"Retrying ({attempt}/{max_retries}) after idle/stop...")
        time.sleep(60)
    raise RuntimeError("Training failed after retries.")

if __name__ == "__main__":
    run_training()
```

---

### 3.4 Deterministic sibling-repo spread for writes (upload)

When uploading enriched parquet files, compute `hash(slug) % N` to pick one of N sibling repos (e.g., `data-0`, `data-1`, ..., `data-N-1`). This spreads write load and avoids single-repo throttling.

```python
import hashlib

def pick_sibling_repo(slug, n_repos=4, base_name="data"):
    idx = int(hashlib.md5(slug.encode()).hexdigest(), 
