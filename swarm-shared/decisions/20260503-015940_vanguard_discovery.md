# vanguard / discovery

## Final synthesized plan (correct + actionable)

### 1. Diagnosis (merged, corrected)
- **No persisted manifest**: every training/data-selection run triggers authenticated `list_repo_tree` against the HF API, burning quota and risking 429s.  
- **Schema/casting hazards**: data loader likely uses `load_dataset(streaming=True)` or repeated per-file API calls across heterogeneous repos, causing `pyarrow.CastError` from mixed schemas.  
- **Lightning Studio churn**: reuse is not enforced; new runs create new studios, wasting quota (≈80 hr/mo).  
- **Authenticated data loading**: training depends on HF API during data loading instead of using CDN-only fetches (`https://huggingface.co/datasets/{repo}/resolve/main/...`).  
- **No orchestration guardrails**: if a studio stops (idle-stop), training dies instead of restarting on prioritized cloud machines (L40S → H200).

### 2. Single change: CDN-first manifest + reuse guardrails
Create **one** durable utility module and a small training-side loader that together:
- Build/persist `(repo, dateFolder) → file-list` manifest once (or on TTL) via a single non-recursive `list_repo_tree` call.  
- Drive all training data loading via CDN URLs only (zero authenticated HF API calls during training).  
- Enforce schema normalization at parse time to avoid `pyarrow.CastError`.  
- Reuse running Lightning Studios when available; otherwise start with minimal, deterministic configs.  
- Provide CLI + importable API and simple verification steps.

### 3. Implementation

**File**: `/opt/axentx/vanguard/discovery/manifest.py`

```python
#!/usr/bin/env python3
"""
Generate and use a CDN-first file manifest for Hugging Face datasets.

- Manifest: <repo>/<dateFolder>/file_manifest.json
- Training uses CDN URLs only (no HF API calls during data load).
- Lightweight, retry-aware, and TTL-cached.
"""
import argparse
import io
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

try:
    from huggingface_hub import HfApi
except Exception:  # pragma: no cover - soft dependency for runtime
    HfApi = None

MANIFEST_NAME = "file_manifest.json"
HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
MANIFEST_TTL_SECONDS = 7 * 86400  # 7 days


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_manifest(
    repo: str,
    date_folder: str,
    out_dir: Optional[Path] = None,
) -> Path:
    """
    Build (repo, date_folder) -> [files] manifest using one HF API call.
    Saves to out_dir/repo/date_folder/file_manifest.json
    """
    if HfApi is None:
        raise RuntimeError("huggingface_hub not installed")

    api = HfApi()
    out_path = (out_dir or Path.cwd()) / repo / date_folder
    out_path.mkdir(parents=True, exist_ok=True)
    manifest_path = out_path / MANIFEST_NAME

    # Reuse if fresh
    if manifest_path.exists() and (time.time() - manifest_path.stat().st_mtime) < MANIFEST_TTL_SECONDS:
        return manifest_path

    # Single non-recursive call
    tree = list(api.list_repo_tree(repo=repo, path=date_folder, recursive=False))
    files: List[Dict] = []
    for node in tree:
        if getattr(node, "type", None) != "file":
            continue
        files.append(
            {
                "repo": repo,
                "folder": date_folder,
                "file": Path(node.path).name,
                "path": node.path,
                "cdn_url": HF_CDN_TEMPLATE.format(repo=repo, path=node.path),
                "size": getattr(node, "size", None),
            }
        )

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at_utc": _now_iso(),
        "generated_by": "vanguard/discovery/manifest.py",
        "files": files,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path


def load_manifest(repo: str, date_folder: str, manifest_root: Path) -> Dict:
    p = manifest_root / repo / date_folder / MANIFEST_NAME
    if not p.exists():
        raise FileNotFoundError(f"Manifest missing: {p}")
    return json.loads(p.read_text())


def lightning_studio_reuse(
    name: str,
    target_machine: str = "L40S",
    fallback_machine: str = "L40S",
) -> Dict:
    """
    Return intent/config to reuse or create a Lightning Studio deterministically.
    Does not auto-create; caller decides when to instantiate.
    """
    try:
        from lightning import Machine, Studio, Teamspace  # type: ignore
    except Exception:
        return {"reuse": False, "create_ok": True, "machine": target_machine}

    for s in Teamspace().studios:
        if s.name == name and s.status == "running":
            return {"reuse": True, "studio": s, "machine": target_machine}

    # If stopped or missing, return minimal create spec.
    return {
        "reuse": False,
        "create_ok": True,
        "name": name,
        "machine": Machine(target_machine, cloud="lightning-public-prod"),
    }


def cdn_samples(
    manifest_path: str,
    shuffle: bool = True,
    max_files: int = -1,
    timeout: int = 30,
):
    """
    Yield normalized {prompt, response} dicts from files listed in manifest
    using CDN URLs only. Parses per-line JSONL and projects known fields.
    """
    manifest = json.loads(Path(manifest_path).read_text())
    files: List[Dict] = manifest.get("files", [])
    if shuffle:
        random.shuffle(files)
    if max_files > 0:
        files = files[:max_files]

    for f in files:
        url = f["cdn_url"]
        try:
            import requests

            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
        except Exception as exc:
            print(f"Skip {url}: {exc}")
            continue

        for line in io.StringIO(resp.text):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue

            # Canonical projection; adapt if dataset differs.
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
            response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
            if prompt and response:
                yield {"prompt": prompt, "response": response}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", type=Path, default=Path("./manifests"))
    args = parser.parse_args()

    mp = build_manifest(args.repo, args.date, args.out)
    print(f"Manifest written: {mp}")
```

**Training-side usage (example snippet)**

```python
from axentx.vanguard.discovery.manifest import cdn_samples, lightning_studio_reuse

# 1) Reuse studio if possible
studio_cfg = lightning_studio_reuse("my-training-studio", target_machine="L40S")
if studio_cfg.get("reuse"):
    print("Reusing running studio")
else:
    print("Will create studio:", studio_cfg["name"])

# 2) CDN-only data stream
manifest_p = "manifests/surrogate-1/2026-04-29/file_manifest.json"
for sample in cdn_samples(manifest_p, shuffle=True, max_files=50):
    # train step
    ...
```

### 4. Verification (concrete steps)

1. Build manifest (Mac or cron):
   ```bash
   cd /opt/axentx/vanguard
   python discovery/manifest.py --repo surrogate-1 --date
