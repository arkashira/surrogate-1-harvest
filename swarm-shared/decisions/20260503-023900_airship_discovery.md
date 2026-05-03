# airship / discovery

## Final Consolidated Implementation (Best of Both Candidates)

**Goal:** Eliminate HF API 429s during ingestion and prevent Lightning Studio idle-timeout quota waste by implementing **CDN-only dataset ingestion** + **Lightning Studio reuse + idle-aware restart**.  
**Ship time:** ≤2h.  
**Scope:** Single orchestrator + two small Python scripts + one dataloader change + executable bash wrapper.

---

## 1) Pre-list HF file paths once (Mac orchestration)

Save file list for a date folder to `filelist.json`. Training will use this and fetch via CDN only (no auth, no rate limit).

```bash
# /opt/axentx/airship/scripts/list_hf_date_folder.sh
#!/usr/bin/env bash
# Usage: ./list_hf_date_folder.sh 2026-04-29 [output.json]
set -euo pipefail
cd /opt/axentx/airship

DATE="${1:-2026-04-29}"
OUT="${2:-filelist.json}"

python3 - "$DATE" "$OUT" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-dataset")
date_folder = sys.argv[1]
out_path = sys.argv[2]

api = HfApi()
items = api.list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=False)

files = []
for it in items:
    if it.rfilename.endswith((".parquet", ".jsonl", ".json")):
        files.append(f"{date_folder}/{it.rfilename}")

with open(out_path, "w") as f:
    json.dump({"repo": HF_REPO, "date": date_folder, "files": files}, f, indent=2)

print(f"Wrote {len(files)} files to {out_path}")
PY
```

Make executable:
```bash
chmod +x /opt/axentx/airship/scripts/list_hf_date_folder.sh
```

---

## 2) CDN-only downloader (zero auth, parallel)

Download listed files directly from HF CDN and project to `{prompt,response}` at parse time.

```python
# /opt/axentx/airship/scripts/download_cdn.py
#!/usr/bin/env python3
"""
Download dataset files from HF CDN (no Authorization).
Project to {prompt, response} at parse time.
"""
import json, os, sys, concurrent.futures as cf
from pathlib import Path
import requests
import pyarrow.parquet as pq
from io import BytesIO

HF_CDN = "https://huggingface.co/datasets"

def download_file(repo: str, file_path: str, out_dir: Path):
    url = f"{HF_CDN}/{repo}/resolve/main/{file_path}"
    out_path = out_dir / Path(file_path).name
    if out_path.exists():
        return str(out_path)

    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    if file_path.endswith(".parquet"):
        table = pq.read_table(BytesIO(resp.content))
        # Keep only needed columns; tolerate missing ones
        cols = [c for c in ("prompt", "response") if c in table.column_names]
        if not cols:
            raise ValueError(f"No prompt/response in {file_path}")
        df = table.select(cols).to_pandas()
        # Ensure consistent column names
        if "prompt" not in df.columns:
            df["prompt"] = ""
        if "response" not in df.columns:
            df["response"] = ""
        df.to_parquet(out_path, index=False)
    else:
        out_path.write_bytes(resp.content)
    return str(out_path)

def main(filelist_path: str, out_dir: str = "data/cdn_raw", workers: int = 8):
    with open(filelist_path) as f:
        meta = json.load(f)

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    repo = meta["repo"]
    files = meta["files"]

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(download_file, repo, f, out_path) for f in files]
        for fut in cf.as_completed(futures):
            try:
                print("Saved:", fut.result())
            except Exception as e:
                print("Failed:", e)

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "filelist.json")
```

Make executable:
```bash
chmod +x /opt/axentx/airship/scripts/download_cdn.py
```

---

## 3) Lightning Studio reuse + idle-aware restart

List running studios, reuse if available, and restart on idle stop without recreating.

```python
# /opt/axentx/airship/scripts/lightning_studio_reuse.py
#!/usr/bin/env python3
"""
Reuse a running Lightning Studio or restart if idle-stopped.
Avoids quota waste from repeated creates.
"""
import os, time, sys
from lightning_sdk import Studio, Teamspace, Machine

STUDIO_NAME = os.getenv("LIGHTNING_STUDIO_NAME", "surrogate-train")
MACHINE = Machine.L40S  # Free tier fallback handled by Lightning priority

def get_running_studio():
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == STUDIO_NAME:
            return s
    return None

def ensure_studio_running():
    studio = get_running_studio()
    if studio is None:
        print(f"Creating studio {STUDIO_NAME}...")
        studio = Studio.create(
            name=STUDIO_NAME,
            machine=MACHINE,
            create_ok=True,
        )
    else:
        print(f"Found studio {STUDIO_NAME} (status={studio.status})")

    # If stopped (idle timeout), restart
    if studio.status != "running":
        print("Studio stopped (likely idle). Restarting...")
        studio.start(machine=MACHINE)
        # Poll until running
        while studio.status != "running":
            time.sleep(10)
            studio.refresh()
            print(f"Status: {studio.status}")

    return studio

if __name__ == "__main__":
    ensure_studio_running()
    print("Studio is running and ready.")
```

Make executable:
```bash
chmod +x /opt/axentx/airship/scripts/lightning_studio_reuse.py
```

---

## 4) Updated surrogate/train.py (CDN-only dataloader stub)

Consume `filelist.json` and load from local CDN cache.

```python
# surrogate/train.py  (excerpt)
import json
from pathlib import Path
from torch.utils.data import Dataset, DataLoader

class CDNJsonlDataset(Dataset):
    def __init__(self, filelist_path):
        with open(filelist_path) as f:
            meta = json.load(f)
        self.repo = meta["repo"]
        self.files = meta["files"]
        self.base_dir = Path("data/cdn_raw")

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        f = self.files[idx]
        local_path = self.base_dir / Path(f).name
        # In practice, load and tokenize here; minimal projection already done
        return {"file": str(local_path)}

def get_dataloader(filelist_path, batch_size=8):
    ds = CDNJsonlDataset(filelist_path)
    return DataLoader(ds, batch_size=batch_size, shuffle=True)
```

---

## 5) Orchestration script (executable)

Run the full cycle: list → CDN download → ensure studio → train.

```bash
#!/usr/bin/env bash
# /opt/axentx/airship/scripts/run_training_cycle.sh
set -euo pipefail

cd /opt/axentx/airship

DATE="${1:-2026-04-29}"
FILELIST="filelist.json"

echo "== Listing HF files for $DATE =="
./scripts/list_hf_date_folder.sh "$DATE" "$FILELIST"

echo "== Downloading via CDN =="
./scripts/download_cdn.py "$FILELIST"

echo "== Ensuring Lightning Studio =="
./scripts/lightning_studio_reuse.py

echo "== Starting training =
