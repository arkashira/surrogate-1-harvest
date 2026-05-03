# vanguard / quality

## Final Synthesis (Best Parts + Corrected + Actionable)

**Diagnosis (merged, corrected)**
- No persisted `(repo, dateFolder)` manifest exists → every training run re-enumerates via authenticated HF API, burning quota and risking 429.
- Training likely uses recursive `list_repo_tree` or `load_dataset(streaming=True)` on heterogeneous repos, triggering `pyarrow` schema errors and redundant API calls.
- Ingestion writes mixed-schema files to `enriched/` with extra metadata columns (`source`, `ts`) instead of clean `{prompt, response}` parquet, violating Surrogate-1 schema rules.
- Lightning Studio reuse is not implemented; jobs likely recreate studios each run, wasting quota.
- No CDN bypass strategy: training uses authenticated API paths instead of public CDN URLs, keeping rate-limit exposure high during long epochs.

**Proposed change (merged, prioritized)**
Create `/opt/axentx/vanguard/training/manifest.py` and patch `/opt/axentx/vanguard/training/train.py` to:
- Add `build_manifest(repo, date_folder)` → writes `manifests/{repo}__{date_folder}.json` containing only file paths (one API call per folder, non-recursive).
- Replace runtime enumeration in training with manifest-only CDN fetches (`hf_hub_download` or raw CDN URLs) and project to `{prompt, response}` at parse time.
- Reuse a running Lightning Studio if available; otherwise start one with `L40S` in `lightning-public-prod`.
- Accept `MANIFEST_PATH` env var so CI/local runs are deterministic.
- Add a small util patch to enforce `{prompt, response}` projection in any existing ingestion writer (if `utils/` exists).

Scope: two new files (`manifest.py`, `train.py`) + one small util patch if `utils/` exists; no changes to ingestion or cron wrappers.

---

## Implementation

```bash
# /opt/axentx/vanguard/training/manifest.py
import os
import json
from pathlib import Path
from huggingface_hub import HfApi

MANIFEST_DIR = Path(__file__).parent.parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

def build_manifest(repo: str, date_folder: str, output_path: str | None = None) -> str:
    """
    Single authenticated call to list one date folder (non-recursive).
    Returns local path to written manifest JSON.
    Manifest format:
      {
        "repo": "...",
        "date_folder": "...",
        "files": ["file1.parquet", "sub/file2.parquet", ...]
      }
    """
    api = HfApi()
    # Non-recursive to avoid pagination explosion; we only need one date folder
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [entry.path for entry in tree if entry.type == "file"]

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "files": sorted(files),
    }

    if output_path is None:
        safe_repo = repo.replace("/", "__")
        output_path = MANIFEST_DIR / f"{safe_repo}__{date_folder}.json"
    else:
        output_path = Path(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2))
    return str(output_path)


def load_manifest(manifest_path: str):
    return json.loads(Path(manifest_path).read_text())
```

```python
# /opt/axentx/vanguard/training/train.py
import os
import json
from pathlib import Path
from typing import Iterator, Tuple

import pyarrow.parquet as pq
import pandas as pd
import requests
from huggingface_hub import hf_hub_download

from .manifest import load_manifest

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"

def iter_cdn_parquet(manifest_path: str, repo: str, local_cache: str | None = None) -> Iterator[Tuple[str, bytes]]:
    """
    Yield (file_path, parquet_bytes) using CDN URLs (no auth, no API rate-limit).
    If local_cache is provided, files are cached locally via hf_hub_download fallback.
    Projects to {prompt, response} at parse time; ignores extra columns.
    """
    manifest = load_manifest(manifest_path)
    for file_path in manifest["files"]:
        if local_cache:
            # authenticated but cached; avoids repeated CDN if cache hit
            local_file = hf_hub_download(repo_id=repo, filename=file_path, cache_dir=local_cache)
            yield file_path, Path(local_file).read_bytes()
        else:
            url = CDN_TEMPLATE.format(repo=repo, file_path=file_path)
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            yield file_path, resp.content


def parse_parquet_to_pairs(parquet_bytes: bytes):
    """
    Surrogate-1 schema enforcement:
    - Accept any input schema.
    - Require at least one text-like field for prompt and one for response.
    - Map common aliases -> {prompt, response}.
    - Return list of {prompt, response}.
    """
    import io
    table = pq.read_table(io.BytesIO(parquet_bytes))
    df = table.to_pandas()

    # Heuristic field mapping
    prompt_candidates = [c for c in df.columns if "prompt" in str(c).lower()]
    response_candidates = [c for c in df.columns if "response" in str(c).lower() or "completion" in str(c).lower()]

    if not prompt_candidates or not response_candidates:
        # fallback: first text col = prompt, second = response
        text_cols = [c for c in df.columns if pd.api.types.is_string_dtype(df[c])]
        if len(text_cols) < 2:
            raise ValueError("Cannot infer prompt/response fields")
        prompt_col, response_col = text_cols[0], text_cols[1]
    else:
        prompt_col, response_col = prompt_candidates[0], response_candidates[0]

    pairs = []
    for _, row in df.iterrows():
        pairs.append({
            "prompt": str(row[prompt_col]).strip(),
            "response": str(row[response_col]).strip(),
        })
    return pairs


def build_lightning_studio():
    """
    Reuse running studio if exists; else start L40S in lightning-public-prod.
    """
    from lightning import Studio, Machine, Teamspace
    teamspace = Teamspace()
    existing = [s for s in teamspace.studios if s.name == "vanguard-train" and s.status == "Running"]
    if existing:
        return existing[0]

    # Free tier fallback: L40S in public cloud
    machine = Machine(
        name="vanguard-train",
        cloud="lightning-public-prod",
        type="L40S",
    )
    studio = Studio(
        name="vanguard-train",
        machine=machine,
        create_ok=True,
    )
    return studio


def run_training_step(manifest_path: str, repo: str):
    """
    Minimal training loop using CDN-only fetches and schema projection.
    """
    for file_path, data in iter_cdn_parquet(manifest_path, repo):
        pairs = parse_parquet_to_pairs(data)
        # Replace with actual surrogate-1 training logic.
        print(f"Parsed {len(pairs)} pairs from {file_path}")
        # Example: accumulate for batch training
        # ...


if __name__ == "__main__":
    # Example usage (Mac orchestration only)
    repo = os.getenv("HF_REPO", "axentx/surrogate-1-data")
    date_folder = os.getenv("DATE_FOLDER", "batches/mirror-merged/2026-04-29")
    manifest_path = os.getenv("MANIFEST_PATH")

    if not manifest_path:
        from .manifest import build_manifest
        manifest_path = build_manifest(repo, date_folder)

    run_training_step(manifest_path, repo)
```

If `utils/` exists, add a small helper patch to enforce `{prompt, response}` projection in any existing ingestion writer.

---

## Verification

1. Build manifest once (Mac orchestration):
   ```bash
   cd /opt/axentx/vanguard
   HF_REPO=axentx/surrogate-1-data
