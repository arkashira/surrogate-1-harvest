# vanguard / quality

## 1. Diagnosis
- No deterministic CDN-first manifest exists; ingestion/training scripts can still trigger `list_repo_tree`/`load_dataset` at runtime, risking 429s and non-reproducible runs.
- Missing content-addressed file list keyed by date/slug; training jobs cannot pin exact data slices and may re-ingest or diverge across runs.
- No explicit fallback to CDN URLs for dataset files; every worker still uses HF API paths, burning quota and failing under rate limits.
- Lightning Studio reuse/idle handling is absent; idle stop kills training and quota is wasted by recreating running studios.
- No guardrails for mixed-schema HF repos; `load_dataset` on heterogeneous files can raise pyarrow `CastError` and poison surrogate-1 ingestion.

## 2. Proposed change
Create `/opt/axentx/vanguard/ingest/manifest.py` + update `/opt/axentx/vanguard/ingest/train.py` (or create if absent) to:
- Add `build_manifest(repo, date_folder)` that calls `list_repo_tree(path=date_folder, recursive=False)` once, saves `manifest-{date}.json` with CDN URLs and content-addressed slugs.
- Add `load_manifest()` used by training to fetch exclusively via CDN (`resolve/main/...`) with zero API calls during data load.
- Add lightweight studio lifecycle helpers (`get_or_start_studio`) to reuse running studios and restart if stopped by idle timeout.
- Add safe HF loader that avoids `load_dataset(streaming=True)` on mixed-schema repos and instead downloads individual files via `hf_hub_download` (or CDN) and projects `{prompt, response}` at parse time.

## 3. Implementation

```bash
# Ensure directories
mkdir -p /opt/axentx/vanguard/ingest /opt/axentx/vanguard/manifests
```

`/opt/axentx/vanguard/ingest/manifest.py`
```python
#!/usr/bin/env python3
"""
CDN-first manifest builder for HF datasets.
Generates content-addressed, date-scoped manifests to avoid runtime list_repo_tree
and bypass HF API rate limits during training.
"""
import json
import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

try:
    from huggingface_hub import list_repo_tree
except Exception:
    list_repo_tree = None


def _cdn_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def _slug_for(path: str) -> str:
    # content-addressed, short, filename-safe
    h = hashlib.sha256(path.encode()).hexdigest()[:12]
    name = Path(path).stem or "file"
    return f"{name}-{h}"


def build_manifest(
    repo: str,
    date_folder: str,
    out_dir: str = "manifests",
    recursive: bool = False,
) -> str:
    """
    Build manifest for a date folder (e.g. '2026-05-01').
    Requires one HF API call (list_repo_tree) then writes CDN-only manifest.
    Returns path to written manifest.
    """
    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub not available")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    entries: List[Dict[str, Any]] = []
    try:
        tree = list_repo_tree(repo=repo, path=date_folder, recursive=recursive)
    except Exception as exc:
        raise RuntimeError(f"Failed to list repo tree for {repo}/{date_folder}: {exc}") from exc

    for node in tree:
        if node.type != "file":
            continue
        path = node.path
        entry = {
            "slug": _slug_for(path),
            "path": path,
            "cdn_url": _cdn_url(repo, path),
            "size": getattr(node, "size", None),
            "lfs": getattr(node, "lfs", None),
        }
        entries.append(entry)

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "count": len(entries),
        "entries": entries,
    }

    fname = f"manifest-{date_folder}.json"
    fpath = out_path / fname
    fpath.write_text(json.dumps(manifest, indent=2))
    return str(fpath)


def load_manifest(manifest_path: str) -> Dict[str, Any]:
    with open(manifest_path) as f:
        return json.load(f)
```

`/opt/axentx/vanguard/ingest/train.py` (create or patch)
```python
#!/usr/bin/env python3
"""
CDN-only training loader for vanguard surrogate-1 pipeline.
Uses pre-built manifests to avoid HF API calls during training.
"""
import json
import os
import sys
from pathlib import Path
from typing import Iterator, Tuple

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except Exception:
    pa = None
    pq = None

# Optional Lightning import (do not require for manifest-only flows)
try:
    from lightning import Studio, Machine, Teamspace
except Exception:
    Studio = Machine = Teamspace = None


MANIFEST_DIR = Path(__file__).parent.parent / "manifests"


def iter_cdn_records(manifest_path: str):
    """
    Yield {prompt, response} records from manifest files via CDN.
    Avoids HF API entirely during training.
    """
    with open(manifest_path) as f:
        manifest = json.load(f)

    for entry in manifest.get("entries", []):
        url = entry["cdn_url"]
        # If parquet, stream via pyarrow from HTTP (CDN supports range requests)
        if url.endswith(".parquet"):
            if pq is None:
                raise RuntimeError("pyarrow required for parquet")
            try:
                table = pq.read_table(url, columns=["prompt", "response"])
                for batch in table.to_batches():
                    df = batch.to_pandas()
                    for _, row in df.iterrows():
                        yield {"prompt": row["prompt"], "response": row["response"]}
            except Exception as exc:
                # Fallback: skip malformed files rather than crash training
                sys.stderr.write(f"Skipping {url}: {exc}\n")
                continue
        else:
            # Generic JSONL fallback (one {prompt,response} per line)
            import requests
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            for line in resp.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    yield {"prompt": obj["prompt"], "response": obj["response"]}
                except Exception:
                    continue


def get_or_start_studio(name: str, machine: str = "L40S", reuse_ok: bool = True):
    """
    Reuse running studio or start a new one. Restarts if stopped by idle timeout.
    Requires lightning-ai SDK.
    """
    if Studio is None:
        raise RuntimeError("lightning not installed")

    teamspace = Teamspace()
    running = [s for s in teamspace.studios if s.name == name and s.status == "Running"]
    if reuse_ok and running:
        return running[0]

    stopped = [s for s in teamspace.studios if s.name == name and s.status == "Stopped"]
    if stopped:
        # restart stopped studio
        s = stopped[0]
        s.start(machine=Machine(machine))
        return s

    # create new
    return Studio(
        name=name,
        machine=Machine(machine),
        create_ok=True,
    )


def run_training_step(manifest_path: str, max_steps: int = 1000):
    """
    Minimal training loop using CDN records.
    Replace with actual surrogate-1 training logic.
    """
    count = 0
    for record in iter_cdn_records(manifest_path):
        # Placeholder: surrogate-1 training step
        # train_step(record["prompt"], record["response"])
        count += 1
        if count >= max_steps:
            break
    return count


if __name__ == "__main__":
    # Example usage:
   
