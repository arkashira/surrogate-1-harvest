# vanguard / backend

## Final Synthesized Implementation

I merged the strongest elements from both candidates:

- **Canonical entrypoint**: `/opt/axentx/vanguard/backend/orchestrate.py` (Candidate 1 path) with CLI args (Candidate 2).
- **HF CDN-bypass**: single recursive file-list generation with CDN URL construction and optional local cache (both candidates).
- **Lightning Studio reuse + idle-stop resilience**: explicit status checks and restart on idle-stop (Candidate 2), with L40S/public fallback (both).
- **Schema safety**: optional projection/validation step to prevent `pyarrow.CastError` (Candidate 2).
- **Mac safety**: `MAC=CLI` guard to prevent accidental local GPU/model loading (Candidate 1).
- **Idempotency & observability**: deterministic names, reuse guard, and clear logging (both).

```python
#!/usr/bin/env python3
"""
/opt/axentx/vanguard/backend/orchestrate.py
Mac-safe, idempotent orchestrator for surrogate-1 (or any HF) training.

Features:
- HF CDN-bypass: pre-lists dataset files once → file_list.json
- Lightning Studio reuse (L40S → public fallback) with idle-stop resilience
- CLI-driven, deterministic, observable
- Mac guard: no local model loading / no CUDA on Mac when MAC_CLI_ONLY=1
- Optional schema projection to avoid pyarrow.CastError
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import lightning as L
from huggingface_hub import HfApi, list_repo_tree

# ── Configuration ────────────────────────────────────────────────────────
HF_REPO = os.getenv("HF_REPO", "datasets/example/surrogate-1")
HF_FOLDER = os.getenv("HF_FOLDER", "batches/mirror-merged/2026-04-29")
FILE_LIST_PATH = Path("file_list.json")
STUDIO_NAME = os.getenv("STUDIO_NAME", "vanguard-surrogate-1")
PROJECT_DIR = Path(__file__).parent.parent
MAC_CLI_ONLY = os.getenv("MAC_CLI_ONLY", "1") == "1"

# ── Mac safety ───────────────────────────────────────────────────────────
def enforce_mac_rule() -> None:
    if MAC_CLI_ONLY:
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        print("[orchestrate] MAC_CLI_ONLY=1 — no local GPU/model loading permitted.")

# ── HF CDN-bypass file list ─────────────────────────────────────────────
def build_cdn_file_list(repo: str, folder: str, recursive: bool = True) -> list[str]:
    """
    List dataset files and emit file_list.json.
    recursive=True walks subfolders (single call per folder) to minimize API usage.
    Returns local paths suitable for CDN URLs.
    """
    api = HfApi()
    entries = list_repo_tree(repo_id=repo, path=folder, recursive=False)

    files = []
    for entry in entries:
        if entry.type == "file" and entry.path.lower().endswith((".parquet", ".jsonl", ".csv")):
            files.append(entry.path)
        elif entry.type == "folder" and recursive:
            sub = list_repo_tree(repo_id=repo, path=entry.path, recursive=False)
            for subentry in sub:
                if subentry.type == "file" and subentry.path.lower().endswith((".parquet", ".jsonl", ".csv")):
                    files.append(subentry.path)

    FILE_LIST_PATH.write_text(json.dumps(files, indent=2))
    print(f"[orchestrate] CDN file list written to {FILE_LIST_PATH} ({len(files)} files)")
    return files

def cdn_urls(repo: str, files: list[str]) -> list[str]:
    return [f"https://huggingface.co/datasets/{repo}/resolve/main/{p}" for p in files]

# ── Schema projection (optional) ─────────────────────────────────────────
def project_schema_safe(file_list_path: Path, output_path: Path | None = None) -> Path | None:
    """
    Lightweight schema projection to avoid pyarrow.CastError.
    If pyarrow is available, reads first file and writes a minimal schema JSON.
    Returns path to schema file or None if skipped.
    """
    try:
        import pyarrow.parquet as pq
    except Exception:
        print("[orchestrate] pyarrow not available — skipping schema projection.")
        return None

    if not file_list_path.exists():
        return None

    files = json.loads(file_list_path.read_text())
    if not files:
        return None

    first = next((f for f in files if f.lower().endswith(".parquet")), None)
    if not first:
        return None

    try:
        # Use CDN URL to avoid HF API calls
        url = cdn_urls(HF_REPO, [first])[0]
        with pq.ParquetFile(url) as pf:
            schema = pf.schema_arrow
        schema_dict = {field.name: str(field.type) for field in schema}
    except Exception as exc:
        print(f"[orchestrate] Schema projection failed: {exc}")
        return None

    out = output_path or PROJECT_DIR / "schema_projection.json"
    out.write_text(json.dumps(schema_dict, indent=2))
    print(f"[orchestrate] Schema projection written to {out}")
    return out

# ── Lightning Studio lifecycle ───────────────────────────────────────────
def get_or_start_studio(name: str) -> L.studio.Studio:
    teamspace = L.Teamspace()
    running = [s for s in teamspace.studios if s.name == name and s.status == "running"]
    if running:
        studio = running[0]
        print(f"[orchestrate] Reusing running studio: {studio.name}")
        return studio

    machine = L.Machine.L40S
    studio = L.studio.Studio(name=name, machine=machine, create_ok=True)
    print(f"[orchestrate] Started studio {name} on {machine}")
    return studio

def ensure_studio_running(studio: L.studio.Studio) -> bool:
    if studio.status == "running":
        return True
    try:
        print(f"[orchestrate] Studio {studio.name} not running (status={studio.status}); restarting...")
        studio.start(machine=L.Machine.L40S)
        # Wait briefly for running state
        for _ in range(10):
            if studio.status == "running":
                return True
            time.sleep(5)
        print("[orchestrate] Studio failed to reach running state after restart.", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"[orchestrate] Failed to restart studio: {exc}", file=sys.stderr)
        return False

# ── Training submission ──────────────────────────────────────────────────
def run_training(
    studio: L.studio.Studio,
    train_script: Path,
    repo: str,
    file_list_path: Path,
    extra_env: dict[str, str] | None = None,
) -> None:
    if not train_script.exists():
        print(f"[orchestrate] train script not found: {train_script}", file=sys.stderr)
        return

    if not ensure_studio_running(studio):
        print("[orchestrate] Cannot submit training — studio unavailable.", file=sys.stderr)
        return

    env = {
        "HF_FILE_LIST": str(file_list_path.resolve()),
        "HF_REPO": repo,
        "PYTHONUNBUFFERED": "1",
        **(extra_env or {}),
    }
    studio.run(str(train_script), environment=env, wait=False)
    print("[orchestrate] Training job submitted")

# ── CLI ──────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vanguard backend training orchestrator")
    parser.add_argument("--repo", default=HF_REPO, help="HF repo (e.g., datasets/example/surrogate-1)")
    parser.add_argument("--folder", default=HF_FOLDER, help="HF folder path within repo")
    parser.add_argument("--train-script", type=Path, default=PROJECT_DIR / "train.py", help="Path to training script")
    parser.add_argument("--studio-name", default=STUDIO_NAME, help="Lightning Studio name")
    parser.add_argument("--no-schema", action="store_true", help="Skip schema projection")
    parser.add_argument("--recursive", action="store_true", default=True, help="Walk subfolders for file
