# vanguard / backend

## Final Synthesis (single, actionable answer)

**Core diagnosis (agreed across candidates)**
- No persisted `(repo, dateFolder) → file-list` manifest → repeated authenticated `list_repo_tree` → burns HF quota and risks 429s.
- Training/data loader likely uses `load_dataset(streaming=True)` or repeated per-file loads on heterogeneous repos → `pyarrow.CastError` from mixed schemas.
- No CDN-only fetch path: authenticated API calls are used during data loading instead of public CDN URLs.
- Lightning Studio reuse is missing: jobs recreate or fail to detect running studios → quota waste and idle-stop deaths.
- No deterministic sibling-repo spread for HF commits → ingestion writes target a single repo and will hit the 128/hr commit cap.

**Chosen strategy (correctness + concrete actionability)**
1. Persist a single manifest per `(repo, dateFolder)` after one `list_repo_tree` call and reuse it everywhere.
2. During training, use CDN-only URLs (`https://huggingface.co/datasets/{repo}/resolve/main/...`) and avoid authenticated API calls in the data path.
3. Project heterogeneous files to `{prompt, response}` at parse/shard-creation time so training never sees mixed schemas.
4. Detect and reuse a running Lightning Studio; restart only if stopped, and pin a stable machine type to avoid idle-stop churn.
5. Deterministically hash slugs across 5 sibling repos to spread commits and stay under the 128/hr cap.

**File-level changes**

```bash
# /opt/axentx/vanguard/backend/manifest.py
import json
import hashlib
import os
from pathlib import Path
from typing import List, Optional
from huggingface_hub import HfApi, list_repo_tree

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

def _manifest_path(repo: str, date_folder: str) -> Path:
    safe = repo.replace("/", "_")
    return MANIFEST_DIR / f"{safe}_{date_folder}.json"

def save_manifest(repo: str, date_folder: str, file_paths: List[str]) -> None:
    _manifest_path(repo, date_folder).write_text(
        json.dumps({repo: {date_folder: file_paths}}, indent=2), encoding="utf-8"
    )

def load_manifest(repo: str, date_folder: str) -> Optional[List[str]]:
    p = _manifest_path(repo, date_folder)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get(repo, {}).get(date_folder)

def build_manifest_once(repo: str, date_folder: str, token: Optional[str] = None) -> List[str]:
    cached = load_manifest(repo, date_folder)
    if cached is not None:
        return cached

    api = HfApi(token=token)
    # Top-level only to minimize pagination/API use; expand if nested date subfolders needed.
    items = list_repo_tree(repo=repo, path=date_folder, recursive=False, token=token)
    file_paths = [it.rfilename for it in items if it.type == "file"]
    save_manifest(repo, date_folder, file_paths)
    return file_paths

def sibling_repo_for(slug: str, n_siblings: int = 5) -> str:
    # Deterministic sibling selection: hash slug -> pick repo index
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    idx = h % n_siblings
    org, base = "vanguard", "datasets"
    if idx == 0:
        return f"{org}/{base}"
    return f"{org}/{base}-sibling{idx}"
```

```python
# /opt/axentx/vanguard/backend/orchestrate.py
import json
import os
from pathlib import Path
from typing import List

import lightning as L
from huggingface_hub import hf_hub_download, HfApi

from .manifest import build_manifest_once, sibling_repo_for, _manifest_path

HF_REPO = os.getenv("HF_DATASET_REPO", "vanguard/datasets")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", "2026-05-03")
HF_TOKEN = os.getenv("HF_TOKEN", None)

def cdn_urls_for_manifest(repo: str, date_folder: str) -> List[str]:
    """Return public CDN URLs for files (no auth required during training)."""
    files = build_manifest_once(repo, date_folder, token=HF_TOKEN)
    return [
        f"https://huggingface.co/datasets/{repo}/resolve/main/{f}"
        for f in files
    ]

def project_to_pair(raw: dict) -> dict[str, str]:
    """
    Lightweight projection to {prompt,response} for heterogeneous files.
    Keep minimal to avoid pyarrow/mixed-schema issues in training.
    """
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def prepare_local_shard(urls: List[str], out_dir: Path) -> List[Path]:
    """Download via CDN (no auth) and project JSONs to pairs; leave parquet untouched."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for u in urls:
        fname = u.split("/")[-1]
        local = out_dir / fname
        if not local.exists():
            # hf_hub_download uses CDN when possible; token only for private repos
            hf_hub_download(
                repo_id=HF_REPO,
                filename=f"{DATE_FOLDER}/{fname}",
                repo_type="dataset",
                cache_dir=out_dir,
                token=HF_TOKEN,
            )
            # Resolve cached file
            cached = list(out_dir.rglob(fname))
            if cached:
                local = cached[0]

        if local.suffix == ".json":
            raw = json.loads(local.read_text(encoding="utf-8"))
            pair = project_to_pair(raw)
            pair_path = out_dir / f"{local.stem}_pair.json"
            pair_path.write_text(json.dumps(pair, ensure_ascii=False), encoding="utf-8")
            paths.append(pair_path)
        else:
            # Parquet or other: keep as-is; training should select columns explicitly
            paths.append(local)
    return paths

def get_running_studio(name: str) -> "L.studio.Studio | None":
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "running":
            return s
    return None

def run_training_with_cdn_and_reuse() -> None:
    urls = cdn_urls_for_manifest(HF_REPO, DATE_FOLDER)
    workdir = Path("/tmp/vanguard_shard")
    prepare_local_shard(urls, workdir)

    train_py = str(Path(__file__).parent / "train.py")
    studio_name = "vanguard-train"
    studio = get_running_studio(studio_name)

    if studio is None:
        # Create or reuse stopped studio; pin machine to reduce idle-stop churn
        from lightning.pytorch.studio import Machine
        studio = L.studio.Studio(
            name=studio_name,
            script_path=train_py,
            create_ok=True,
        )
        studio.start(machine=Machine.L40S)

    # Pass manifest and workdir via env so train.py does CDN-only, schema-clean loads
    manifest_p = _manifest_path(HF_REPO, DATE_FOLDER)
    env = {
        "VANGUARD_MANIFEST_PATH": str(manifest_p),
        "VANGUARD_WORKDIR": str(workdir),
    }
    studio.run(env=env)
```

```python
# /opt/axentx/vanguard/backend/train.py  (minimal, robust usage)
import os
import json
from pathlib import Path
from typing import List, Dict

def load_pairs_from_manifest() -> List[Dict[str, str]]:
    """
    Load pre-projected {prompt,response} pairs from workdir.
    Avoids load_dataset on heterogeneous schemas and uses CDN-prepared shards.
    """
    workdir = Path(os.environ["VANGUARD_WORKDIR"])
    pairs = []
    for f in workdir.glob("*_pair.json"):
        pairs.append(json
