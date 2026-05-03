# airship / frontend

## Implementation Plan (≤2h)

**Highest-value change**: Add CDN-only dataset loader + Lightning idle-resilient runner to `/opt/axentx/airship/surrogate/train.py` (create if missing) and a companion `scripts/list_hf_files.py` so training never hits HF API during data loading and survives Lightning idle stops.

### Concrete steps
1. Create `scripts/list_hf_files.py` — Mac-side script that calls `list_repo_tree` once per date folder and writes `file_list.json` into the repo (committed or embedded).
2. Create/update `surrogate/train.py` — Lightning training entrypoint that:
   - Loads `file_list.json`
   - Uses `hfcdn_download()` (raw `https://huggingface.co/datasets/.../resolve/main/...`) with zero Authorization headers
   - Projects to `{prompt, response}` on the fly
   - Wraps `Teamspace.studios` reuse + idle-stop detection + auto-restart on L40S
3. Add `requirements.txt` line for `lightning` if missing.
4. Ensure executable bits and Bash shebangs for any wrapper scripts.

---

### 1) `scripts/list_hf_files.py` (run from Mac)

```python
#!/usr/bin/env python3
"""
Run from Mac (or any dev machine) after HF API rate-limit window clears.
Writes file_list.json for a given date folder so Lightning training can
fetch via CDN only (zero API calls during training).
"""
import json
import os
import sys
from datetime import datetime, timezone

# prefer lightning/huggingface_hub if installed; fallback to requests+pagination
try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUT_FILE = os.getenv("OUT_FILE", "surrogate/file_list.json")

def main() -> None:
    api = HfApi()
    # non-recursive per folder to avoid 100x pagination on big repos
    tree = api.list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    files = []
    for entry in tree:
        if entry.type == "file":
            files.append(f"{DATE_FOLDER}/{entry.path}")
    payload = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    os.makedirs(os.path.dirname(OUT_FILE) if os.path.dirname(OUT_FILE) else ".", exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} files to {OUT_FILE}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x scripts/list_hf_files.py
```

---

### 2) `surrogate/train.py` (Lightning entrypoint)

```python
#!/usr/bin/env python3
"""
Lightning Studio training script (surrogate).
- Uses CDN-only dataset fetches (no HF API auth/rate-limit during training).
- Projects heterogeneous files to {prompt, response}.
- Reuses running Studio and auto-restarts on idle-stop.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterator, List

import requests
import torch
from torch.utils.data import Dataset, DataLoader

try:
    from lightning import LightningWork, LightningApp, Teamspace
except ImportError:
    print("Install: pip install lightning")
    sys.exit(1)

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-dataset")
FILE_LIST_PATH = os.getenv("FILE_LIST_PATH", "surrogate/file_list.json")
CDN_BASE = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

# ---------- Dataset ----------
class HFCDNDataset(Dataset):
    """
    Lightweight dataset that streams JSONL/CSV/text files from HF CDN
    and projects to {prompt, response}.
    """
    def __init__(self, file_urls: List[str], max_files: int = -1):
        self.file_urls = file_urls if max_files < 0 else file_urls[:max_files]

    @staticmethod
    def _project_to_pair(raw: str) -> Dict[str, str]:
        # Best-effort projection: expect JSON lines with prompt/response or simple Q/A pairs.
        try:
            obj = json.loads(raw.strip())
            if "prompt" in obj and "response" in obj:
                return {"prompt": str(obj["prompt"]), "response": str(obj["response"])}
            # fallback keys
            for pkey in ("question", "input", "instruction"):
                for rkey in ("answer", "output", "completion"):
                    if pkey in obj and rkey in obj:
                        return {"prompt": str(obj[pkey]), "response": str(obj[rkey])}
        except Exception:
            pass
        # If format unknown, treat whole line as prompt, empty response
        return {"prompt": raw.strip(), "response": ""}

    def __len__(self) -> int:
        return len(self.file_urls)

    def __getitem__(self, idx: int) -> Dict[str, str]:
        url = self.file_urls[idx]
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        text = resp.text
        # naive line-by-line for JSONL; adapt as needed per file extension
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return {"prompt": "", "response": ""}
        # return first valid pair (could be extended to return lists)
        return self._project_to_pair(lines[0])

def hfcdn_download(file_path: str) -> str:
    """Download single file via CDN (no auth)."""
    url = f"{CDN_BASE}/{file_path.lstrip('/')}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.text

def load_file_list() -> List[str]:
    if not os.path.exists(FILE_LIST_PATH):
        raise FileNotFoundError(
            f"{FILE_LIST_PATH} not found. Run scripts/list_hf_files.py first."
        )
    with open(FILE_LIST_PATH, encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("files", [])

# ---------- Lightning Work ----------
class SurrogateTrainer(LightningWork):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = None

    def run(self) -> None:
        # 1) Load file list once (embedded by list_hf_files.py)
        try:
            files = load_file_list()
        except Exception as e:
            print(f"Failed to load file list: {e}")
            return

        if not files:
            print("No files found in file_list.json")
            return

        # Build CDN URLs
        urls = [f"{CDN_BASE}/{f}" for f in files]
        dataset = HFCDNDataset(urls, max_files=int(os.getenv("MAX_FILES", "-1")))
        loader = DataLoader(dataset, batch_size=1, num_workers=0)

        # 2) Minimal training loop placeholder (replace with real model/tokenizer)
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print(f"Using device={device}, samples={len(dataset)}")

        for i, batch in enumerate(loader):
            # Example: print pairs (replace with actual training step)
            print(f"[{i}] prompt={batch['prompt'][0][:60]}... response={batch['response'][0][:60]}...")
            if i >= int(os.getenv("MAX_STEPS", "10")) - 1:
                break

        print("Training step completed (placeholder).")

# ---------- App + Studio reuse + idle resilience ----------
def ensure_running_studio() -> Teamspace:
    """
    Reuse running Studio if present; otherwise start one on L40S.
    Avoids recreating and burning quota.
    """
    for studio in Teamspace.studios:

