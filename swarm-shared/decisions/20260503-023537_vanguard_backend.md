# vanguard / backend

## Final synthesized implementation

**Core diagnosis (merged)**
- Authenticated HF API calls (`list_repo_tree`, `load_dataset`) during training burn quota and cause 429s.
- No static file manifest: every run re-enumerates the repo via API.
- No CDN bypass: training fetches via `/api/` instead of public `resolve/main/` URLs.
- Missing resilient retry/backoff and no graceful fallback to CDN when API limits are hit.
- Risk of accidental local heavy compute on dev machines (especially macOS) instead of delegating to Lightning/Kaggle/Cerebras.

**Chosen approach**
- Single, idempotent manifest generation (orchestrator-only) that lists a date folder once and saves `file_manifest.json`.
- Training uses **CDN-only** fetches with no authenticated API calls and no `load_dataset`.
- Exponential backoff + long sleep on 429 for the one allowed API call (manifest build).
- Explicit guardrails to prevent local heavy compute and a clear orchestrator/training boundary.
- Minimal, deterministic training loop that streams parquet shards via CDN and projects columns.

---

## 1. Backend: `data_loader.py`

```python
# /opt/axentx/vanguard/backend/data_loader.py
import json
import os
import time
import hashlib
import requests
from pathlib import Path
from typing import List, Dict, Optional, Iterator

try:
    from huggingface_hub import list_repo_tree
    HF_HUB_AVAILABLE = True
except Exception:
    HF_HUB_AVAILABLE = False

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-dataset")
HF_BRANCH = os.getenv("HF_BRANCH", "main")
MANIFEST_PATH = Path(__file__).parent / "file_manifest.json"
CDN_BASE = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

# ---------- CDN utilities (no auth) ----------
def _cdn_url(rel_path: str) -> str:
    return f"{CDN_BASE}/{rel_path.lstrip('/')}"

def fetch_via_cdn(rel_path: str, timeout: int = 30) -> bytes:
    url = _cdn_url(rel_path)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

# ---------- Resilient API (only for manifest build) ----------
def api_call_with_backoff(fn, *args, max_retries: int = 5, **kwargs):
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            is_429 = status == 429
            if is_429 and attempt < max_retries - 1:
                wait = 2 ** attempt
                if wait < 60:
                    wait = 60
                if attempt >= 2:
                    wait = 360
                print(f"[HF API 429] sleeping {wait}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            raise

# ---------- Manifest management ----------
def build_manifest(date_folder: str, out_path: Path = MANIFEST_PATH) -> List[str]:
    """Single API call to list files for one date folder; idempotent write."""
    if not HF_HUB_AVAILABLE:
        raise RuntimeError("huggingface_hub not available")
    tree = api_call_with_backoff(list_repo_tree, repo_id=HF_REPO, path=date_folder, recursive=False)
    files = sorted(item.rfilename for item in tree if not item.rfilename.endswith("/"))
    manifest = {"date_folder": date_folder, "files": files}
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path} ({len(files)} files)")
    return files

def load_manifest() -> List[str]:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest missing: {MANIFEST_PATH}. Run build_manifest(date_folder) first.")
    data = json.loads(MANIFEST_PATH.read_text())
    return data["files"]

# ---------- Streaming parquet via CDN ----------
def stream_local_parquet_shards(
    files: List[str],
    project_to: Optional[List[str]] = None,
    batch_size: int = 1000
) -> Iterator[Dict]:
    """
    Stream rows from parquet files via CDN, one file at a time (low memory).
    Yields rows projected to project_to columns.
    """
    project_to = project_to or ["prompt", "response"]
    try:
        import pyarrow.parquet as pq
    except ImportError as e:
        raise ImportError("pyarrow required for parquet projection") from e

    for rel in files:
        if not rel.endswith(".parquet"):
            continue
        content = fetch_via_cdn(rel)
        table = pq.read_table(content, columns=project_to)
        df = table.to_pandas()
        for _, row in df.iterrows():
            yield {k: row[k] for k in project_to if k in row}

# ---------- Compute guardrails ----------
def guard_no_local_heavy_compute():
    """
    Prevent accidental heavy local compute on dev machines.
    Allow only when explicitly running orchestration on macOS or when
    running in an approved remote environment.
    """
    env = os.getenv("VANGUARD_ENV", "")
    platform_system = os.getenv("VANGUARD_PLATFORM", os.uname().sysname if hasattr(os, "uname") else "unknown")

    # Approved remote runners
    approved_envs = {"lightning", "kaggle", "cerebras", "remote", "orchestrator"}
    if env.lower() in approved_envs:
        return

    # If on macOS and not orchestrator, block heavy ops
    if platform_system == "Darwin" and env != "orchestrator":
        raise RuntimeError(
            "Local heavy compute on macOS not allowed. "
            "Set VANGUARD_ENV=orchestrator for manifest generation only. "
            "Training must run on Lightning/Kaggle/Cerebras (VANGUARD_ENV=lightning|kaggle|cerebras)."
        )
```

---

## 2. Training entrypoint: `train.py`

```python
# /opt/axentx/vanguard/backend/train.py
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data_loader import load_manifest, stream_local_parquet_shards, guard_no_local_heavy_compute

# ---------- Training step (replace with your surrogate-1 logic) ----------
def train_step(batch: Dict) -> Dict:
    # Minimal placeholder: integrate your LightningModule or optimizer here.
    # Example: loss = model(batch["prompt"], batch["response"])
    return {"loss": 0.0}

# ---------- Training loop ----------
def run_training(
    date_folder: str = "batches/mirror-merged/2026-04-29",
    max_steps: Optional[int] = None
) -> None:
    guard_no_local_heavy_compute()

    manifest_path = Path(__file__).parent / "file_manifest.json"
    if not manifest_path.exists():
        if os.getenv("VANGUARD_ALLOW_MANIFEST_BUILD") == "1":
            from data_loader import build_manifest
            print("Building manifest (single API call)...")
            build_manifest(date_folder)
        else:
            raise RuntimeError(
                "file_manifest.json missing. Generate it once on the orchestrator:\n"
                "  VANGUARD_ENV=orchestrator python -c \"from data_loader import build_manifest; build_manifest('<date_folder>')\""
            )

    files = load_manifest()
    print(f"Loaded {len(files)} files from manifest. Streaming via CDN (no auth HF API calls)...")

    step = 0
    for sample in stream_local_parquet_shards(files, project_to=["prompt", "response"]):
        result = train_step(sample)
        step += 1
        if max_steps is not None and step >= max_steps:
            break
        if step % 1000 == 0:
            print(f"step={step} loss={result.get('loss', 'n/a')}")

    print(f"Done. Processed {step} samples via CDN (zero authenticated HF API calls during training).")

if __name__ == "__main__":
    # For local smoke
