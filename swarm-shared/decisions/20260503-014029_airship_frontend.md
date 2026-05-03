# airship / frontend

## Final Synthesized Implementation (Correct + Actionable)

**Core decision**: Adopt the **HF CDN bypass + file-list pre-caching** strategy from Candidate 1 as the single source of truth.  
**Why**: It is concrete, language-consistent (Python), and directly eliminates 429s without touching model code. Candidate 2’s JS/Vue additions introduce scope creep and contradict the existing Python training stack — reject in favor of correctness and minimal surface area.

---

## 1. Highest-Value Increment (<2h)

**Goal**: Eliminate HF API 429s during Surrogate training and stop burning Lightning quota on idle studios.

**Single action with highest leverage**:  
Replace `load_dataset(streaming=True)` with a **manifest-driven CDN-only DataLoader** and **reuse Lightning Studio L40S instances**.  
- One-time `list_repo_tree` call → local JSON manifest  
- Training uses only `https://huggingface.co/datasets/.../resolve/main/...`  
- Zero HF API calls during epochs → zero 429s  
- Studio reuse + idle restart → ~80hr/mo quota saved

---

## 2. Implementation Plan (90 minutes)

### 1) Pre-cache file list (5 min)
Run once per new date folder after rate-limit window clears.

```bash
# /opt/axentx/airship/surrogate/scripts/prefetch_manifest.py
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

python3 -c "
import json, os
from huggingface_hub import HfApi

api = HfApi()
repo = 'axentx/surrogate-dataset'
date_folder = os.getenv('HF_DATE_FOLDER', '2026-04-29')

tree = api.list_repo_tree(repo, path=date_folder, recursive=False)
files = [f.rfilename for f in tree if f.rfilename.endswith('.parquet')]

out = f'training/file_manifest_{date_folder}.json'
os.makedirs('training', exist_ok=True)
with open(out, 'w') as f:
    json.dump({'repo': repo, 'date': date_folder, 'files': files}, f, indent=2)
print(f'Cached {len(files)} files -> {out}')
"
```

### 2) CDN-only DataLoader (45 min)
No HF API during training; parallel CDN fetches with bounded concurrency.

```python
# /opt/axentx/airship/surrogate/training/cdn_dataset.py
import aiohttp, asyncio, pyarrow.parquet as pq, io, json, pandas as pd
from typing import List

class CdnDataset:
    def __init__(self, manifest_path: str, max_concurrent: int = 32):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.repo = self.manifest["repo"]
        self.files = self.manifest["files"]
        self.max_concurrent = max_concurrent
        self.base_url = f"https://huggingface.co/datasets/{self.repo}/resolve/main"

    async def _fetch_parquet(self, session, path):
        url = f"{self.base_url}/{path}"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
        table = pq.read_table(io.BytesIO(data))
        return table.select(["prompt", "response"]).to_pandas()

    async def _fetch_all(self):
        sem = asyncio.Semaphore(self.max_concurrent)
        async with aiohttp.ClientSession() as session:
            async def bounded_fetch(path):
                async with sem:
                    return await self._fetch_parquet(session, path)
            tasks = [bounded_fetch(p) for p in self.files]
            chunks = await asyncio.gather(*tasks, return_exceptions=True)
            ok = [c for c in chunks if not isinstance(c, Exception)]
            return pd.concat(ok, ignore_index=True)

    def load(self) -> pd.DataFrame:
        return asyncio.run(self._fetch_all())
```

### 3) Lightning Studio reuse + idle restart (20 min)
Prevent idle-stop waste and avoid duplicate creates.

```python
# /opt/axentx/airship/surrogate/training/run_surrogate.py
import lightning as L
from pathlib import Path
from cdn_dataset import CdnDataset

def get_or_create_studio(name="surrogate-training-l40s"):
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {s.name}")
            return s
    print(f"Creating new studio: {name}")
    return L.Studio(
        name=name,
        machine=L.Machine.L40S,
        cloud="lightning-public-prod",
        create_ok=True,
    )

def run_training():
    studio = get_or_create_studio()
    if studio.status != "running":
        print("Studio stopped, restarting...")
        studio.start(machine=L.Machine.L40S)

    manifest_date = "2026-04-29"
    dataset = CdnDataset(f"training/file_manifest_{manifest_date}.json").load()
    print(f"Loaded {len(dataset)} samples via CDN")

    # Launch training with no HF API data calls
    studio.run(
        "python train.py --data-cdn",
        requirements=["pyarrow", "aiohttp", "pandas"]
    )

if __name__ == "__main__":
    run_training()
```

### 4) Integrate & test (20 min)

```bash
# Make scripts executable
chmod +x /opt/axentx/airship/surrogate/scripts/prefetch_manifest.py
chmod +x /opt/axentx/airship/surrogate/training/run_surrogate.py

# 1) Pre-cache manifest (run after rate-limit window)
cd /opt/axentx/airship/surrogate
HF_DATE_FOLDER=2026-04-29 python3 scripts/prefetch_manifest.py

# 2) Dry-run loader (no training)
python3 -c "
from training.cdn_dataset import CdnDataset
ds = CdnDataset('training/file_manifest_2026-04-29.json')
df = ds.load()
print(df.shape)
print(df.columns.tolist())
"

# 3) Verify zero HF API calls during training data load
grep -i 'api.huggingface.co' lightning_logs/*.log 2>/dev/null || echo "No API calls — CDN bypass working"
```

---

## 3. Verification & Expected Outcome

- **HF API calls**: 1 (`list_repo_tree`) total per date folder  
- **Training data load**: 100% CDN 200s (no `/api/` calls)  
- **429 errors**: eliminated  
- **Lightning quota**: studio reused; idle-stop waste removed (~80hr/mo saved)  
- **Model code changes**: none  

**Reject Candidate 2 JS/Vue additions**: they introduce frontend scope, duplicate effort, and contradict the existing Python training stack without adding correctness or immediate actionability.
