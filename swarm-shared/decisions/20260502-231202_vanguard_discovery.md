# vanguard / discovery

## 1. Diagnosis
- No durable ingestion manifest: every training run re-lists HF repos and re-downloads, causing 429s and wasted bandwidth.
- Training loads via HF API (`load_dataset`/`list_repo_files`) instead of CDN bypass → guaranteed rate limits during data loading.
- No Studio reuse guard: scripts recreate or start new Lightning Studios instead of reusing running ones, burning 80+ hrs/mo quota.
- No idle-stop protection: Lightning Studio idle timeout kills long-running training; no auto-restart on stopped state.
- Missing deterministic routing for multi-repo writes: HF commit cap (128/hr/repo) not mitigated; ingestion can block on single repo.

## 2. Proposed change
Add a lightweight discovery/orchestration module that:
- Persists a file manifest (JSON) per date folder after a single HF API tree list.
- Provides a train loader that uses only CDN URLs (zero API calls during training).
- Reuses running Lightning Studios by name and auto-restarts if stopped.
- Routes HF writes across 5 sibling repos by hash-slug.

Scope: create `/opt/axentx/vanguard/vanguard/discovery.py` and update `/opt/axentx/vanguard/train.py` to use it.

## 3. Implementation

```bash
# ensure project layout
mkdir -p /opt/axentx/vanguard/vanguard
touch /opt/axentx/vanguard/vanguard/__init__.py
```

`/opt/axentx/vanguard/vanguard/discovery.py`
```python
import json
import hashlib
import os
from pathlib import Path
from typing import List, Optional, Dict
import requests

try:
    from lightning import Lightning, Teamspace, Machine, Studio
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False
    Lightning = Teamspace = Machine = Studio = None

HF_DATASETS_BASE = "https://huggingface.co/datasets"
MANIFEST_DIR = Path(os.getenv("VANGUARD_MANIFEST_DIR", "/opt/axentx/vanguard/manifests"))
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

HF_SIBLING_REPOS = [
    "axentx/vanguard-ingest-0",
    "axentx/vanguard-ingest-1",
    "axentx/vanguard-ingest-2",
    "axentx/vanguard-ingest-3",
    "axentx/vanguard-ingest-4",
]


def _hash_slug(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16)


def pick_sibling_repo(slug: str) -> str:
    """Deterministic repo selection for HF commit-cap mitigation."""
    idx = _hash_slug(slug) % len(HF_SIBLING_REPOS)
    return HF_SIBLING_REPOS[idx]


def list_and_persist_tree(repo: str, date_folder: str, token: Optional[str] = None) -> Dict:
    """
    Single API call to list a date folder and persist manifest.
    Requires huggingface_hub>=0.23 for list_repo_tree.
    """
    from huggingface_hub import list_repo_tree

    repo_path = f"{repo}/{date_folder}"
    manifest_path = MANIFEST_DIR / f"{repo.replace('/', '_')}_{date_folder}.json"

    if manifest_path.exists():
        return json.loads(manifest_path.read_text())

    items = list(list_repo_tree(repo=repo, path=date_folder, recursive=False))
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "files": [
            {"path": item.path, "size": getattr(item, "size", None)}
            for item in items
            if item.type == "file"
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def cdn_urls_for_manifest(manifest: Dict) -> List[str]:
    """Return CDN URLs for files in manifest (no auth required)."""
    repo = manifest["repo"]
    base = f"{HF_DATASETS_BASE}/{repo}/resolve/main"
    urls = []
    for f in manifest["files"]:
        urls.append(f"{base}/{f['path']}")
    return urls


def load_parquet_shard_cdn(url: str):
    """Load a single parquet shard from CDN (zero HF API calls)."""
    import pandas as pd
    return pd.read_parquet(url)


def project_to_prompt_response(df):
    """Project raw parquet to {prompt, response} only."""
    # Heuristic: keep only prompt/response cols; rename if needed.
    cols = [c for c in df.columns if c in {"prompt", "response", "instruction", "completion"}]
    if not cols:
        # fallback: first two text-like cols
        text_cols = [c for c in df.columns if df[c].dtype == "object"][:2]
        if len(text_cols) == 2:
            df = df.rename(columns={text_cols[0]: "prompt", text_cols[1]: "response"})
            return df[["prompt", "response"]]
        raise ValueError("Cannot project to prompt/response")
    mapping = {}
    if "instruction" in cols and "prompt" not in cols:
        mapping["instruction"] = "prompt"
    if "completion" in cols and "response" not in cols:
        mapping["completion"] = "response"
    if mapping:
        df = df.rename(columns=mapping)
    keep = [c for c in ["prompt", "response"] if c in df.columns]
    return df[keep]


def reuse_or_create_studio(name: str, machine: str = "L40S", timeout_min: int = 10):
    """
    Reuse a running Lightning Studio; if stopped, restart it.
    Returns Studio instance or None if unavailable.
    """
    if not LIGHTNING_AVAILABLE:
        return None

    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name:
            if s.status == "running":
                return s
            if s.status == "stopped":
                s.start(machine=Machine(machine))
                # simple wait
                import time
                for _ in range(timeout_min * 6):
                    time.sleep(10)
                    s.refresh()
                    if s.status == "running":
                        return s
                raise RuntimeError(f"Studio {name} failed to start within {timeout_min}m")
            # other states: treat as unavailable
            return None

    # create if not exists
    return Studio(name=name, create_ok=True, machine=Machine(machine))
```

`/opt/axentx/vanguard/train.py` (minimal update to use CDN + manifest)
```python
import json
from pathlib import Path
from vanguard.discovery import (
    list_and_persist_tree,
    cdn_urls_for_manifest,
    load_parquet_shard_cdn,
    project_to_prompt_response,
)

def build_dataset(date_folder: str = "2026-04-29", repo: str = "axentx/surrogate-1"):
    manifest = list_and_persist_tree(repo=repo, date_folder=date_folder)
    urls = cdn_urls_for_manifest(manifest)

    rows = []
    for url in urls:
        try:
            df = load_parquet_shard_cdn(url)
            df = project_to_prompt_response(df)
            for _, row in df.iterrows():
                rows.append({"prompt": row["prompt"], "response": row["response"]})
        except Exception as e:
            print(f"Skipping {url}: {e}")
            continue
    return rows

if __name__ == "__main__":
    data = build_dataset()
    print(f"Built {len(data)} prompt/response pairs from CDN")
    # continue with Lightning training using `data` (no HF API calls during dataload)
```

## 4. Verification
1. Run manifest creation (single API call):
   ```bash
   cd /opt/axentx/vanguard
   python -c "from vanguard.discovery import list_and_persist_tree; m=list_and_persist_tree('axentx/surrogate-1','2026-04-29'); print('files:', len(m['files']))"
   ```
   - Expect: JSON manifest written to `manifests/` and non-zero file count. No 429 if within window; if 429, wait 360s
