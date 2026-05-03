# vanguard / backend

## 1. Diagnosis

- No persisted `(repo, dateFolder) → file-list` manifest: every training run performs authenticated `list_repo_tree` or `load_dataset` discovery, burning HF API quota and risking 429s.
- Heterogeneous schema ingestion likely uses `load_dataset(streaming=True)` on mixed-file repos, causing `pyarrow.CastError` at train time.
- Training script probably recomputes file lists and schema projection on every run instead of using CDN-only fetches with an embedded file manifest.
- No deterministic repo selection for commit-cap mitigation: writes likely target a single repo and will hit the 128/hr cap.
- Mac/remote boundary unclear: orchestration may attempt local model loading or heavy compute instead of delegating to Lightning/Kaggle/Cerebras.

## 2. Proposed change

Create `/opt/axentx/vanguard/src/data/manifest.py` and update the training launcher to:
- Accept a pre-generated `file_manifest.json` (repo + dateFolder → list of relative paths).
- Use CDN-only downloads during training (`https://huggingface.co/datasets/.../resolve/main/...`).
- Project heterogeneous files to `{prompt, response}` at parse time (never rely on `load_dataset` schema inference).
- Deterministically pick one of 5 sibling repos for writes via hash-slug modulo.

Scope: add one new module + small change to the training script entrypoint (or create a minimal one if missing).

## 3. Implementation

```bash
# Ensure structure
mkdir -p /opt/axentx/vanguard/src/data
mkdir -p /opt/axentx/vanguard/src/train
```

```python
# /opt/axentx/vanguard/src/data/manifest.py
import json
import hashlib
import os
from pathlib import Path
from typing import List, Dict, Optional
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

HF_DATASETS_BASE = "https://huggingface.co/datasets"
SIBLING_REPOS = [
    "axentx/vanguard-mirror-0",
    "axentx/vanguard-mirror-1",
    "axentx/vanguard-mirror-2",
    "axentx/vanguard-mirror-3",
    "axentx/vanguard-mirror-4",
]

def pick_sibling_repo(slug: str) -> str:
    """Deterministic repo selection for commit-cap mitigation."""
    digest = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return SIBLING_REPOS[digest % len(SIBLING_REPOS)]

def build_manifest(repo: str, date_folder: str, out_path: Path) -> Dict:
    """
    One-time Mac-side helper: authenticated list_repo_tree call (run sparingly).
    Persists manifest to be embedded in training.
    """
    try:
        from huggingface_hub import HfApi
    except ImportError:
        raise RuntimeError("huggingface_hub required for manifest generation only")

    api = HfApi()
    tree = api.list_repo_tree(repo, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "files": sorted(files),
        "cdn_base": f"{HF_DATASETS_BASE}/{repo}/resolve/main/{date_folder}"
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest

def load_manifest(manifest_path: Path) -> Dict:
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text())

def cdn_url(repo: str, date_folder: str, file_path: str) -> str:
    return f"{HF_DATASETS_BASE}/{repo}/resolve/main/{date_folder}/{file_path}"

def stream_cdn_file(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def project_to_pair(raw_bytes: bytes, file_path: str) -> Dict[str, str]:
    """
    Lightweight projection for heterogeneous files.
    Extend per format (jsonl, json, txt, parquet via pyarrow).
    Returns {prompt, response}.
    """
    suffix = Path(file_path).suffix.lower()
    if suffix == ".jsonl":
        import json as _json
        lines = raw_bytes.decode("utf-8").strip().splitlines()
        objs = [_json.loads(l) for l in lines if l.strip()]
        # Best-effort field mapping
        prompt_field = next((k for k in objs[0].keys() if "prompt" in k.lower()), "prompt")
        response_field = next((k for k in objs[0].keys() if "response" in k.lower() or "completion" in k.lower()), "response")
        # For multi-row files, concatenate or pick last; here we pick last for simplicity
        last = objs[-1]
        return {
            "prompt": str(last.get(prompt_field, "")),
            "response": str(last.get(response_field, ""))
        }
    elif suffix == ".json":
        import json as _json
        obj = _json.loads(raw_bytes)
        prompt_field = next((k for k in obj.keys() if "prompt" in k.lower()), "prompt")
        response_field = next((k for k in obj.keys() if "response" in k.lower() or "completion" in k.lower()), "response")
        return {
            "prompt": str(obj.get(prompt_field, "")),
            "response": str(obj.get(response_field, ""))
        }
    elif suffix in {".txt", ".md"}:
        text = raw_bytes.decode("utf-8")
        # Simple split: first paragraph as prompt, remainder as response
        parts = text.split("\n\n", 1)
        return {
            "prompt": parts[0].strip() if parts else "",
            "response": parts[1].strip() if len(parts) > 1 else ""
        }
    else:
        # Fallback: treat entire file as response with empty prompt
        return {
            "prompt": "",
            "response": raw_bytes.decode("utf-8", errors="replace")
        }

def build_dataset_from_manifest(
    manifest_path: Path,
    limit: Optional[int] = None,
    max_workers: int = 8
):
    """
    Generator yielding {prompt, response} using CDN-only downloads.
    No authenticated HF API calls during training.
    """
    manifest = load_manifest(manifest_path)
    files = manifest["files"]
    if limit:
        files = files[:limit]
    repo = manifest["repo"]
    date_folder = manifest["date_folder"]

    def fetch_one(file_path):
        url = cdn_url(repo, date_folder, file_path)
        try:
            raw = stream_cdn_file(url)
            pair = project_to_pair(raw, file_path)
            pair["_source_file"] = file_path
            return pair
        except Exception as exc:
            return {"_error": str(exc), "_source_file": file_path}

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_one, f): f for f in files}
        for fut in as_completed(futures):
            result = fut.result()
            if "_error" not in result:
                yield result
```

```python
# /opt/axentx/vanguard/src/train/train.py  (minimal launcher if none exists)
import argparse
from pathlib import Path
from src.data.manifest import build_dataset_from_manifest

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True, help="Path to file_manifest.json")
    parser.add_argument("--limit", type=int, default=None, help="Limit samples for dev run")
    parser.add_argument("--output-parquet", type=Path, default=None, help="Optional: save projected pairs")
    args = parser.parse_args()

    pairs = list(build_dataset_from_manifest(args.manifest, limit=args.limit))
    print(f"Projected {len(pairs)} pairs from manifest.")

    if args.output_parquet:
        try:
            import pandas as pd
            df = pd.DataFrame(pairs)
            args.output_parquet.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(args.output_parquet, index=False)
            print(f"
