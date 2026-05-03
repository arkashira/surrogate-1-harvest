# vanguard / discovery

## Final Synthesis — One Correct, Actionable Plan

**Core diagnosis (merged, de-duplicated):**
- No persisted `(repo, dateFolder) → file-list` manifest → every training run triggers authenticated `list_repo_tree`, burning HF API quota and risking 429s.
- Training uses `load_dataset(streaming=True)` or repeated per-file loads on heterogeneous repos → `pyarrow.CastError` from mixed schemas.
- Authenticated API calls are used for file fetches when public CDN URLs would bypass rate limits entirely.
- Surrogate-1 schema hygiene is missing: raw files with attribution/metadata columns are written to `enriched/` instead of projecting to `{prompt, response}` only and moving attribution to filename/metadata.
- No reuse guard for Lightning Studio: training script likely recreates studios instead of listing/running existing ones, wasting quota.

**Chosen strategy (resolve contradictions in favor of correctness + actionability):**
- Build **one manifest per `(repo, dateFolder)`** with a single authenticated `list_repo_tree` call and store it under `manifests/`.
- **Training must never call HF API again**; fetch files exclusively via public CDN URLs.
- **Project to `{prompt, response}` at parse time**; reject or drop extra columns to avoid schema conflicts. Do not use `load_dataset(streaming=True)` on heterogeneous repos.
- **Reuse running Lightning Studio when available**; create only if none exists and stopped.
- Keep implementation minimal, robust, and deterministic so it can run in CI/Lightning and locally.

---

## 1. Manifest utility (`/opt/axentx/vanguard/discovery/manifest.py`)

```python
#!/usr/bin/env python3
"""
Build and use a repo+date manifest to avoid HF API rate limits during training.
- Single list_repo_tree (authenticated) -> saved JSON manifest.
- Training uses CDN-only URLs (no auth).
- Deterministic shard selection to bypass HF commit/concurrency caps.
"""
import json
import os
import hashlib
import time
from pathlib import Path
from typing import Dict, List, Optional

try:
    from huggingface_hub import list_repo_tree
except Exception:  # pragma: no cover — graceful fallback for envs without HF hub
    list_repo_tree = None  # type: ignore

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

def _slug(repo: str, date_folder: str) -> str:
    return f"{repo.replace('/', '_')}-{date_folder}"

def manifest_path(repo: str, date_folder: str) -> Path:
    return MANIFEST_DIR / f"{_slug(repo, date_folder)}.json"

def build_manifest(repo: str, date_folder: str, token: Optional[str] = None) -> List[str]:
    """
    Single authenticated list_repo_tree call (non-recursive).
    Returns list of file paths and saves manifest.
    """
    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub is required to build manifest")

    path = date_folder.strip("/")
    tree = list_repo_tree(repo=repo, path=path, recursive=False, token=token)
    files = sorted(item.rfilename for item in tree if getattr(item, "type", None) == "file")

    entry = {
        "repo": repo,
        "date_folder": date_folder,
        "created_ts": int(time.time()),
        "files": files,
    }

    p = manifest_path(repo, date_folder)
    p.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    return files

def load_manifest(repo: str, date_folder: str) -> Dict:
    p = manifest_path(repo, date_folder)
    if not p.exists():
        raise FileNotFoundError(f"Manifest missing: {p}. Run build_manifest first.")
    return json.loads(p.read_text(encoding="utf-8"))

def cdn_urls_for_manifest(manifest: Dict) -> List[str]:
    repo = manifest["repo"]
    return [
        f"https://huggingface.co/datasets/{repo}/resolve/main/{f}"
        for f in manifest["files"]
    ]

def shard_repo_for_slug(slug: str, n_shards: int = 5) -> int:
    """Deterministic shard selection to avoid HF concurrent-commit limits."""
    digest = hashlib.sha256(slug.encode()).hexdigest()
    return int(digest, 16) % n_shards
```

---

## 2. Training launcher (`/opt/axentx/vanguard/discovery/train.py`)

```python
#!/usr/bin/env python3
"""
Lightning-compatible launcher that:
- Uses persisted manifest to avoid HF API calls during training.
- Fetches files via CDN (no auth) and projects to {prompt, response}.
- Reuses running Lightning Studio when available (best-effort guard).
"""
import json
import os
import tempfile
import shutil
import requests
from pathlib import Path
from typing import Dict, List

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except Exception as e:
    raise RuntimeError("pyarrow is required for projection step") from e

from .manifest import build_manifest, load_manifest, cdn_urls_for_manifest

HF_REPO = os.getenv("HF_REPO", "datasets/example")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-05-03")
HF_TOKEN = os.getenv("HF_TOKEN", None)

# Minimal schema projection: keep only prompt/response fields.
# Extend per format as needed (jsonl/parquet/csv).
def project_to_pair(local_path: Path) -> List[Dict[str, str]]:
    suffix = local_path.suffix.lower()
    text = local_path.read_text(encoding="utf-8")
    pairs = []

    if suffix == ".jsonl":
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            prompt = obj.get("prompt") or obj.get("instruction") or ""
            response = obj.get("response") or obj.get("output") or ""
            if prompt and response:
                pairs.append({"prompt": prompt, "response": response})
    elif suffix == ".json":
        obj = json.loads(text)
        if isinstance(obj, list):
            for item in obj:
                prompt = item.get("prompt") or item.get("instruction") or ""
                response = item.get("response") or item.get("output") or ""
                if prompt and response:
                    pairs.append({"prompt": prompt, "response": response})
        else:
            prompt = obj.get("prompt") or obj.get("instruction") or ""
            response = obj.get("response") or obj.get("output") or ""
            if prompt and response:
                pairs.append({"prompt": prompt, "response": response})
    else:
        # CSV/TSV fallback: require 'prompt' and 'response' columns
        import csv
        reader = csv.DictReader(text.strip().splitlines())
        for row in reader:
            prompt = row.get("prompt") or row.get("instruction") or ""
            response = row.get("response") or row.get("output") or ""
            if prompt and response:
                pairs.append({"prompt": prompt, "response": response})
    return pairs

class SurrogateTrainer:
    """
    Lightweight runner (can be wrapped by LightningWork or called from CLI).
    """
    def __init__(self, repo: str, date_folder: str, hf_token: str | None = None):
        self.repo = repo
        self.date_folder = date_folder
        self.hf_token = hf_token

    def run(self, reuse_ok: bool = True) -> Path:
        # 1) Manifest: single HF API call if missing
        mp = manifest_path(self.repo, self.date_folder)
        if not mp.exists():
            print("Building manifest (single authenticated HF API call)...")
            build_manifest(self.repo, self.date_folder, token=self.hf_token)
        manifest = load_manifest(self.repo, self.date_folder)

        # 2) Fetch via CDN only (no auth)
        urls = cdn_urls_for_manifest(manifest)
        tmpdir = tempfile.mkdtemp()
        all_pairs: List[Dict[str, str]] = []

        try:
            for url in urls:
                fname = url.split("/")[-1]
                out = Path(tmpdir) / fname
                resp = requests.get(url, timeout=60)
                resp
