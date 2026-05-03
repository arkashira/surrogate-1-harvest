# vanguard / quality

## Final Synthesized Implementation

### 1. Diagnosis (Consolidated)
- **CDN-bypass missing**: training/preview still use authenticated HF API (`load_dataset`, `list_repo_tree`) → quota burn + 429 risk.
- **No file manifest**: every run re-enumerates repo files via API → redundant calls and fragility.
- **No manifest cache**: repeated UI interactions trigger redundant API calls.
- **Lightning idle-stop hazard**: training can die if studio stops between `.run()` calls; no reuse guard or checkpoint resume.

### 2. Solution Overview
- Generate a **static, CDN-based manifest** once (per date folder) and embed it in training.
- **Preview UI** uses CDN URLs by default; authenticated HF API only as fallback.
- **Training script** consumes the manifest directly, streams via CDN, and adds Lightning idle-stop guard + automatic resume.
- All changes are additive and non-breaking.

### 3. Implementation

#### 3.1 Manifest generator (CDN-bypass, single API call per folder)
```python
# /opt/axentx/vanguard/src/data/manifest.py
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from huggingface_hub import list_repo_tree

CDN_ROOT = "https://huggingface.co/datasets"


def list_date_folder(repo_id: str, date_folder: str, token: str | None = None) -> List[str]:
    """Single non-recursive API call; returns relative parquet paths."""
    tree = list_repo_tree(
        repo_id=repo_id,
        path=date_folder,
        repo_type="dataset",
        token=token,
        recursive=False,
    )
    return [
        f.rfilename
        for f in tree
        if f.type == "file" and f.rfilename.lower().endswith(".parquet")
    ]


def build_manifest(
    repo_id: str,
    date_folder: str,
    token: str | None = None,
    siblings: int = 5,
) -> Dict:
    """
    Build manifest with CDN URLs and deterministic sibling routing.
    Manifest excludes 'source'/'ts' cols; attribution via filename/sibling.
    """
    files = list_date_folder(repo_id, date_folder, token=token)

    items = []
    for fpath in sorted(files):
        slug = Path(fpath).stem
        idx = hash(slug) % siblings
        sibling_repo = f"{repo_id}-sibling-{idx}" if siblings > 1 else repo_id
        cdn_url = (
            f"{CDN_ROOT}/{sibling_repo}/resolve/main/{date_folder}/{Path(fpath).name}"
        )
        items.append(
            {
                "file": fpath,
                "cdn_url": cdn_url,
                "sibling_repo": sibling_repo,
                "slug": slug,
                "date_folder": date_folder,
            }
        )

    return {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "siblings": siblings,
        "files": items,
    }


def save_manifest(manifest: Dict, out_dir: str = "manifests") -> str:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    fname = f"{manifest['date_folder']}.json"
    fpath = out_path / fname
    with open(fpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return str(fpath)


def load_manifest(date_folder: str, manifest_dir: str = "manifests") -> Dict:
    fpath = Path(manifest_dir) / f"{date_folder}.json"
    if not fpath.is_file():
        raise FileNotFoundError(f"Manifest not found: {fpath}")
    with open(fpath, "r", encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":
    repo = os.getenv("HF_DATASET_REPO", "org/vanguard-mirror")
    date = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
    tok = os.getenv("HF_TOKEN", None)
    mf = build_manifest(repo, date, token=tok, siblings=5)
    saved = save_manifest(mf)
    print(f"Manifest saved: {saved}")
```

#### 3.2 Preview UI: CDN-first, authenticated fallback
```python
# /opt/axentx/vanguard/src/ui/preview.py
import json
from pathlib import Path
from typing import List

import pyarrow.parquet as pq
import requests
from huggingface_hub import hf_hub_download

from vanguard.data.manifest import load_manifest

MANIFEST_DIR = Path(__file__).parent.parent / "manifests"


class PreviewLoader:
    def __init__(self, date_folder: str, manifest_dir=MANIFEST_DIR):
        self.manifest = load_manifest(date_folder, str(manifest_dir))

    def sample_cdn(self, n: int = 8) -> List[dict]:
        """Load n files via CDN (no auth) and project {prompt, response}."""
        samples = []
        for item in self.manifest["files"][:n]:
            url = item["cdn_url"]
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                tmp = "/tmp/tmp.parquet"
                with open(tmp, "wb") as f:
                    f.write(r.content)
                df = pq.read_table(tmp).to_pandas()
                prompt = df.iloc[0].get("prompt") or df.iloc[0].get("text") or ""
                response = df.iloc[0].get("response") or df.iloc[0].get("completion") or ""
                samples.append(
                    {
                        "slug": item["slug"],
                        "prompt": prompt,
                        "response": response,
                        "cdn_url": url,
                    }
                )
            except Exception as exc:
                samples.append({"slug": item["slug"], "error": str(exc)})
        return samples

    def sample_hf_fallback(self, n: int = 8, token: str | None = None) -> List[dict]:
        """Fallback: authenticated HF download (rate-limited)."""
        samples = []
        for item in self.manifest["files"][:n]:
            try:
                local = hf_hub_download(
                    repo_id=self.manifest["repo_id"],
                    filename=item["file"],
                    repo_type="dataset",
                    token=token,
                )
                df = pq.read_table(local).to_pandas()
                prompt = df.iloc[0].get("prompt") or df.iloc[0].get("text") or ""
                response = df.iloc[0].get("response") or df.iloc[0].get("completion") or ""
                samples.append(
                    {
                        "slug": item["slug"],
                        "prompt": prompt,
                        "response": response,
                        "source": "hf_api",
                    }
                )
            except Exception as exc:
                samples.append({"slug": item["slug"], "error": str(exc)})
        return samples
```

#### 3.3 Training script: manifest-driven CDN streaming + Lightning idle-stop guard
```python
# /opt/axentx/vanguard/src/training/train.py
import json
from pathlib import Path
from typing import Iterator

import pyarrow.parquet as pq
import requests
import lightning as L
import torch
from torch.utils.data import IterableDataset, DataLoader

MANIFEST_PATH = Path(__file__).parent.parent / "manifests" / "2026-04-29.json"


class CDNParquetIterable(IterableDataset):
    def __init__(self, manifest_path=MANIFEST_PATH, start=0, end=None):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.items = manifest["files"]
        self.end = end if end is not None else len(self.items)
        self.start = start

    def __iter__(self) -> Iterator[dict]:
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            it_start, it_end = self.start, self.end
        else:
            per_worker = (self.end - self.start) // worker_info.num_workers
            wid =
