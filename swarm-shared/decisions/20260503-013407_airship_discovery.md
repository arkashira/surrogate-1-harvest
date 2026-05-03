# airship / discovery

## Final Integrated Implementation  
*(Best parts merged, contradictions resolved for correctness + concrete actionability)*

---

### 1) Core Design (non-negotiable correctness)
- **Single API call** to list one date folder via `list_repo_tree` (repo_type="dataset", recursive=False).  
- **CDN-only fetches** during training: use `https://huggingface.co/datasets/{repo}/resolve/main/{path}` with no Authorization header.  
- **Lightning Studio reuse**: find running studio by name; if stopped → restart on `Lightning/L40S` (free-tier fallback). Never create duplicate studios.  
- **No writes to HF during training**; project to `{prompt, response}` at parse time.  
- **Fail-fast validation**: confirm CDN-only traffic (no `Authorization` header) and studio reuse before marking success.

---

### 2) Backend (`/opt/axentx/airship/surrogate/training/`)

#### `cdn_loader.py`
```python
import json
from pathlib import Path
from typing import List, Dict
import requests
from huggingface_hub import HfApi

HF_API = HfApi()
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_folder(repo_id: str, date_folder: str) -> List[str]:
    """
    Single API call to list files in one date folder (non-recursive).
    Returns repo-relative file paths only (no directories).
    """
    items = HF_API.list_repo_tree(
        repo_id=repo_id,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )
    paths = []
    for it in items:
        name = it["path"] if isinstance(it, dict) else getattr(it, "path", str(it))
        if name.endswith("/"):
            # one-level shallow list for subfolder
            sub_items = HF_API.list_repo_tree(
                repo_id=repo_id,
                path=name,
                repo_type="dataset",
                recursive=False,
            )
            for s in sub_items:
                subname = s["path"] if isinstance(s, dict) else getattr(s, "path", str(s))
                if not subname.endswith("/"):
                    paths.append(subname)
        else:
            paths.append(name)
    return paths

def build_file_list(repo_id: str, date_folder: str, out_json: Path) -> List[Dict]:
    paths = list_date_folder(repo_id, date_folder)
    entries = [
        {
            "repo": repo_id,
            "path": p,
            "cdn_url": CDN_TEMPLATE.format(repo=repo_id, path=p),
        }
        for p in paths
    ]
    out_json.write_text(json.dumps(entries, indent=2))
    return entries

def stream_cdn_file(entry: Dict, chunk_size: int = 8192):
    resp = requests.get(entry["cdn_url"], stream=True, timeout=30)
    resp.raise_for_status()
    for chunk in resp.iter_content(chunk_size=chunk_size):
        yield chunk
```

#### `launcher.py`
```python
import json
import time
from pathlib import Path
from typing import Optional

from lightning import LightningWork, Machine

from .cdn_loader import build_file_list

class TrainingLauncher(LightningWork):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.active_studio = None

    def _find_running_studio(self, name: str):
        # Correct: list studios in this teamspace and pick running one by name
        from lightning import Teamspace
        for s in Teamspace.studios:
            if getattr(s, "name", None) == name and getattr(s, "status", None) == "running":
                return s
        return None

    def start_training(
        self,
        dataset_repo: str,
        date_folder: str,
        script_path: Path,
        studio_name: str = "surrogate-train",
        machine: str = "Lightning/L40S",
    ) -> dict:
        # 1) Build CDN file list once (on launcher node)
        list_path = Path(f"/tmp/filelist_{int(time.time())}.json")
        entries = build_file_list(dataset_repo, date_folder, list_path)
        if not entries:
            raise ValueError("No files found in date folder")

        # 2) Reuse or create studio
        studio = self._find_running_studio(studio_name)
        if studio is None:
            from lightning import Studio
            studio = Studio(
                name=studio_name,
                script_path=str(script_path),
                machine=Machine(machine),
                cloud_compute=Machine(machine),
                create_ok=True,
            )
        else:
            if studio.status != "running":
                studio.start(machine=Machine(machine))

        # 3) Launch run with CDN file list as argument
        run_cfg = {
            "script": str(script_path),
            "args": [
                "--file_list_json", str(list_path),
                "--dataset_repo", dataset_repo,
                "--date_folder", date_folder,
            ],
        }
        studio.run(**run_cfg)
        self.active_studio = studio
        return {
            "studio": studio_name,
            "status": studio.status,
            "files": len(entries),
            "cdn_only": True,
        }
```

#### `train_template.py`
```python
import argparse
import json
from pathlib import Path
from torch.utils.data import IterableDataset, DataLoader
import requests

class CDNIterableDataset(IterableDataset):
    def __init__(self, file_list_json: Path):
        with open(file_list_json) as f:
            self.entries = json.load(f)

    def __iter__(self):
        for entry in self.entries:
            resp = requests.get(entry["cdn_url"], stream=True, timeout=30)
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    yield {"prompt": obj["prompt"], "response": obj["response"]}
                except Exception:
                    continue

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file_list_json", type=Path, required=True)
    parser.add_argument("--dataset_repo", type=str, default="")
    parser.add_argument("--date_folder", type=str, default="")
    args = parser.parse_args()

    dataset = CDNIterableDataset(args.file_list_json)
    loader = DataLoader(dataset, batch_size=None, num_workers=0)

    # Replace with your training loop; example:
    for batch in loader:
        prompt, response = batch["prompt"], batch["response"]
        # training step here
        pass

if __name__ == "__main__":
    main()
```

---

### 3) FastAPI Endpoints (backend)
Add to your existing FastAPI app:

```python
from fastapi import APIRouter
from .surrogate.training.launcher import TrainingLauncher
from .surrogate.training.cdn_loader import list_date_folder

router = APIRouter()
launcher = TrainingLauncher()  # ensure singleton or DI as needed

@router.post("/training/list-cdn-files")
def list_cdn_files(payload: dict):
    repo = payload["dataset_repo"]
    date = payload["date_folder"]
    files = list_date_folder(repo, date)
    return {"files": files}

@router.post("/training/start")
def start_training(payload: dict):
    result = launcher.start_training(
        dataset_repo=payload["dataset_repo"],
        date_folder=payload["date_folder"],
        script_path=Path(payload["script_path"]),
        studio_name=payload.get("studio_name", "surrogate-train"),
        machine=payload.get("machine", "Lightning/L40S"),
    )
    return result
```

---

### 4) Frontend (minimal, action-focused)
Add a training panel with:
- Inputs: Dataset repo ID, date folder (YYYY-MM-DD), script path.
- Button “List CDN Files” → calls `/training/list-cdn-files` → shows file count.
- Button “Start Training” → calls `/training/start` → shows studio name, status, file count, and “CDN-only” badge.
- Live log tail via studio logs (if available) or link to studio.

---

### 5) Validation Checklist (run once)
1. Use a small public dataset repo with one date folder.  
2.
