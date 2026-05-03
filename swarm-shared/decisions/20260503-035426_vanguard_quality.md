# vanguard / quality

## 1. Diagnosis

- Ingestion/training scripts likely re-list HF repos at runtime (no content-addressed manifest), causing 429 rate-limits and non-reproducible runs.
- Mixed-schema files from `dataset-mirror` probably land in `enriched/` without projection to `{prompt,response}`, risking `pyarrow.CastError` during surrogate-1 training.
- No CDN-bypass strategy: training still uses `load_dataset`/`list_repo_files` which hits API auth limits instead of public CDN URLs.
- Lightning Studio reuse not enforced: scripts likely create new studios each run, burning 80+ quota hours/month.
- No pre-flight file manifest: training cannot start until API window clears, making runs flaky and slow.

## 2. Proposed change

Create a small, high-leverage ingestion/training utility that:
- Generates a content-addressed manifest (JSON) for one date folder via a single `list_repo_tree` call.
- Projects mixed-schema files to `{prompt,response}` on read (avoids pyarrow CastError).
- Uses HF CDN URLs exclusively during training (zero API calls while loading data).
- Reuses a running Lightning Studio if present; otherwise starts one (L40S priority).

Scope:
- Add `/opt/axentx/vanguard/scripts/build_manifest.py`
- Add `/opt/axentx/vanguard/scripts/train_surrogate.py` (lightweight loader + stub trainer)
- Add `/opt/axentx/vanguard/scripts/util.py` (reusable helpers)

## 3. Implementation

```bash
# Ensure scripts directory exists
mkdir -p /opt/axentx/vanguard/scripts
```

### util.py
```python
# /opt/axentx/vanguard/scripts/util.py
import json
import os
from pathlib import Path
from typing import List, Dict, Any

try:
    from huggingface_hub import list_repo_tree, hf_hub_download
    from lightning import Lightning, Teamspace, Machine
except ImportError:
    # Graceful fallback for local dev
    list_repo_tree = None
    hf_hub_download = None
    Lightning = None
    Teamspace = None
    Machine = None

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-org/vanguard-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")  # YYYY-MM-DD
MANIFEST_PATH = Path(__file__).parent.parent / "data" / "manifests" / f"{DATE_FOLDER}.json"

def build_manifest() -> List[Dict[str, Any]]:
    """Single API call: list files in one date folder; save manifest."""
    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub not installed")

    tree = list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    files = [
        {"repo": HF_REPO, "path": f.rfilename, "size": f.size}
        for f in tree
        if not f.rfilename.endswith("/")
    ]

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump({"repo": HF_REPO, "date": DATE_FOLDER, "files": files}, f, indent=2)
    return files

def load_manifest() -> Dict[str, Any]:
    if not MANIFEST_PATH.exists():
        build_manifest()
    with open(MANIFEST_PATH) as f:
        return json.load(f)

def project_to_prompt_response(local_path: Path) -> Dict[str, str]:
    """
    Best-effort projection for mixed-schema parquet/jsonl.
    Returns {prompt, response}.
    """
    import pandas as pd

    df = pd.read_parquet(local_path) if str(local_path).endswith(".parquet") else pd.read_json(local_path, lines=True)

    # Common field heuristics
    prompt_col = next((c for c in df.columns if "prompt" in c.lower()), None)
    response_col = next((c for c in df.columns if "response" in c.lower() or "completion" in c.lower()), None)

    if prompt_col is None or response_col is None:
        # Fallback: first two text cols
        text_cols = [c for c in df.columns if df[c].dtype == "object"]
        if len(text_cols) >= 2:
            prompt_col, response_col = text_cols[0], text_cols[1]
        else:
            raise ValueError(f"Cannot project prompt/response from {local_path}")

    return {
        "prompt": df[prompt_col].astype(str).tolist(),
        "response": df[response_col].astype(str).tolist(),
    }

def cdn_url(repo: str, path: str) -> str:
    """Public CDN URL — no auth, bypasses API rate limits."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def reuse_or_start_studio(name: str = "vanguard-surrogate-train"):
    """Reuse running studio; else start L40S (fallback to public tier)."""
    if Lightning is None or Teamspace is None or Machine is None:
        print("Lightning not available — skipping studio management")
        return None

    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s

    print(f"Starting studio: {name}")
    # Priority: L40S in lightning-lambda-prod; fallback to public tier
    try:
        machine = Machine.L40S
    except AttributeError:
        machine = Machine.L40  # public fallback
    return Lightning.Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )
```

### build_manifest.py
```python
# /opt/axentx/vanguard/scripts/build_manifest.py
#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from util import build_manifest

if __name__ == "__main__":
    files = build_manifest()
    print(f"Built manifest with {len(files)} files.")
```

### train_surrogate.py
```python
# /opt/axentx/vanguard/scripts/train_surrogate.py
#!/usr/bin/env python3
"""
Lightning-compatible training script that uses CDN-only fetches.
Embed the manifest produced by build_manifest.py to avoid HF API calls during data load.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import json
import requests
import torch
from torch.utils.data import Dataset, DataLoader
from util import load_manifest, cdn_url, reuse_or_start_studio

class CDNTextDataset(Dataset):
    def __init__(self, manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.items = []
        for finfo in manifest["files"]:
            url = cdn_url(manifest["repo"], finfo["path"])
            # Lightweight: stream and project on the fly (avoid full download if possible)
            # For demo, download small files; for large-scale, use streaming + partial read.
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            local_path = Path("/tmp") / Path(finfo["path"]).name
            local_path.write_bytes(resp.content)
            try:
                projected = self._project(local_path)
                for p, r in zip(projected["prompt"], projected["response"]):
                    self.items.append({"prompt": p, "response": r})
            finally:
                if local_path.exists():
                    local_path.unlink()

    def _project(self, local_path):
        from util import project_to_prompt_response
        return project_to_prompt_response(local_path)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        return self.items[idx]

def dummy_train_step(batch):
    # Placeholder: replace with surrogate-1 tokenizer + model forward
    return {"loss": torch.tensor(0.0)}

def main():
    studio = reuse_or_start_studio("vanguard-surrogate-train")
    if studio is not None and studio.status != "Running":
        # If stopped, restart to avoid idle-timeout death
        studio.start(machine="L40
