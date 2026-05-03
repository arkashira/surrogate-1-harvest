# vanguard / backend

## Final Consolidated Implementation  
*(Best parts merged; contradictions resolved in favor of correctness + concrete actionability)*

### 1. Diagnosis (resolved)
- **Manifest**: no persisted `(repo, dateFolder) → file-list`; every run does authenticated discovery → HF API quota burn + 429 risk.  
  ✅ Fix: single authenticated `list_repo_tree` per `(repo, dateFolder)` → persisted JSON manifest; reuse across runs.
- **Schema/streaming**: `load_dataset(streaming=True)` on heterogeneous repos → `pyarrow.CastError` / schema mismatch.  
  ✅ Fix: bypass HF datasets; fetch via CDN; project to `{prompt, response}` only; strict schema enforcement.
- **CDN/auth**: authenticated API calls during data loading instead of zero-auth CDN.  
  ✅ Fix: use `resolve/main/` CDN URLs; zero auth; retries + backoff.
- **Lightning Studio**: no reuse logic; probable quota waste.  
  ✅ Fix: `get_or_create_studio` + `ensure_studio_running` with idempotent reuse.
- **Idle-stop resilience**: Lightning idle timeout kills training; no pre-run check or auto-restart.  
  ✅ Fix: pre-run status check; restart idle/stopped studios; wait loop with timeout.

### 2. File: `/opt/axentx/vanguard/backend/training/file_manifest.py`
```python
#!/usr/bin/env python3
"""
Persist (repo, dateFolder) -> file-list manifest and provide CDN-only fetches.
"""
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import requests
from huggingface_hub import HfApi  # type: ignore

HF_API = HfApi()
MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

# ---- Manifest ----
def _manifest_path(repo: str, date_folder: str) -> Path:
    safe = repo.replace("/", "_")
    return MANIFEST_DIR / f"manifest_{safe}_{date_folder}.json"

def build_manifest(repo: str, date_folder: str, force: bool = False) -> List[str]:
    """
    Single authenticated list_repo_tree (non-recursive) for one date folder.
    Returns list of relative file paths.
    """
    mp = _manifest_path(repo, date_folder)
    if mp.exists() and not force:
        try:
            return json.loads(mp.read_text())
        except Exception:
            pass  # fallback to rebuild

    items = HF_API.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [it.rfilename for it in items if it.type == "file"]
    mp.write_text(json.dumps(files, indent=2))
    return files

# ---- CDN ----
def cdn_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def fetch_via_cdn(
    repo: str,
    path: str,
    timeout: int = 30,
    max_retries: int = 3,
    backoff_factor: float = 0.5,
) -> bytes:
    """Zero-auth CDN fetch (bypasses /api/ rate limits)."""
    url = cdn_url(repo, path)
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == max_retries:
                raise
            sleep_time = backoff_factor * (2 ** (attempt - 1))
            # simple backoff without extra imports
            import time
            time.sleep(sleep_time)
    raise RuntimeError("unreachable")

# ---- Projection ----
def project_to_prompt_response(raw_bytes: bytes, file_ext: str = ".jsonl") -> List[Dict[str, str]]:
    """
    Parse heterogeneous files and project to {prompt, response} only.
    Supports .jsonl lines with optional extra fields.
    """
    import io
    import json as _json

    out: List[Dict[str, str]] = []
    stream = io.BytesIO(raw_bytes)

    if file_ext == ".jsonl":
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                obj = _json.loads(line)
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
                response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
                if prompt or response:
                    out.append({"prompt": str(prompt), "response": str(response)})
            except Exception:
                continue
    else:
        # fallback: treat whole file as single prompt
        out.append({"prompt": raw_bytes.decode("utf-8", errors="replace"), "response": ""})
    return out
```

### 3. File: `/opt/axentx/vanguard/backend/training/studio_utils.py`
```python
#!/usr/bin/env python3
"""
Lightning Studio reuse + idle-stop resilience.
"""
from lightning_sdk import Lightning, Machine  # type: ignore
from typing import Optional
import time

LIGHTNING = Lightning()

def get_or_create_studio(
    name: str,
    machine: Machine = Machine.L40S,
    create_ok: bool = True,
) -> Optional[Lightning.Teamspace.Studio]:
    for s in LIGHTNING.teamspace.studios:
        if s.name == name:
            return s
    if create_ok:
        return LIGHTNING.teamspace.create_studio(name=name, machine=machine)
    return None

def ensure_studio_running(
    studio: Lightning.Teamspace.Studio,
    machine: Machine = Machine.L40S,
    max_wait: int = 120,
) -> bool:
    if studio.status == "running":
        return True

    if studio.status == "stopped":
        studio.start(machine=machine)
    else:
        # idle/unknown: stop then start for clean state
        try:
            studio.stop()
        except Exception:
            pass
        time.sleep(5)
        studio.start(machine=machine)

    waited = 0
    while waited < max_wait:
        studio.refresh()
        if studio.status == "running":
            return True
        time.sleep(5)
        waited += 5
    return False
```

### 4. File: `/opt/axentx/vanguard/backend/training/train.py`
```python
#!/usr/bin/env python3
"""
Training entrypoint that uses manifest + CDN + studio reuse.
"""
import json
import os
import sys
from pathlib import Path

# Add backend/training to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.training.file_manifest import build_manifest, fetch_via_cdn, project_to_prompt_response
from backend.training.studio_utils import ensure_studio_running, get_or_create_studio
from lightning_sdk import Machine

HF_REPO = os.getenv("HF_REPO", "datasets/axentx/surrogate-1")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")
MANIFEST_FORCE = os.getenv("MANIFEST_FORCE", "0") == "1"

def build_dataset() -> list:
    files = build_manifest(HF_REPO, DATE_FOLDER, force=MANIFEST_FORCE)
    examples = []
    for f in files:
        try:
            raw = fetch_via_cdn(HF_REPO, f)
            ext = Path(f).suffix
            examples.extend(project_to_prompt_response(raw, file_ext=ext))
        except Exception as exc:
            print(f"Skipping {f}: {exc}", file=sys.stderr)
            continue
    return examples

def run_training():
    studio = get_or_create_studio("vanguard-surrogate-1", machine=Machine.L40S, create_ok=True)
    if not studio:
        print("Failed to get/create studio", file=sys.stderr)
        sys.exit(1)

    if not ensure_studio_running(studio, machine=Machine.L40S):
        print("Studio failed to start", file=sys.stderr)
        sys.exit(1)

    dataset = build_dataset()
    out_path = Path(__file__).parent / "train_ready.parquet"
    import pandas as pd
    pd.DataFrame(dataset).to
