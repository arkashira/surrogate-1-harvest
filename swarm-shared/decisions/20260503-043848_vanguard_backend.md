# vanguard / backend

## Final Synthesized Solution

### Diagnosis (merged)
- **No content-addressed manifest per date folder** → runtime `list_repo_files`/`load_dataset` trigger HF API 429s and break reproducibility.
- **Missing deterministic `{path, sha256, size}` snapshot** → CDN-only fetches cannot be validated or resumed; training restarts on failure and epochs are non-reproducible.
- **Training/ingestion enumerate repos at runtime** → wastes quota, risks rate limits, and prevents CDN-only streaming.
- **No repo-to-sibling deterministic routing** → ingestion bursts can hit HF commit limits (128/hr/repo).
- **No Lightning Studio reuse guard** → each training run risks quota waste via recreation instead of reuse.
- **No integrity validation** for local cache/CDN downloads → silent corruption risk across long runs.

---

### Proposed Change (merged + prioritized)
Create two focused deliverables:

1. **Manifest generator** (single source of truth)  
   - Path: `/opt/axentx/vanguard/backend/manifest.py`  
   - Accepts repo + date folder and produces `manifest-{date}.json` with `{path, sha256, size}` via **one** `list_repo_tree` call.  
   - Embeds file list so training uses **CDN-only fetches** (zero API calls during data load).  
   - Supports both HF repo manifests and local mirror manifests (`batches/mirror-merged/{date}/manifest.json`).  
   - Includes deterministic repo-to-sibling routing (`hash(slug) % N`) for commit-cap scaling.  
   - Includes integrity helpers: validate local files against manifest; resume/retry CDN downloads with hash check.

2. **Lightning Studio reuse helper**  
   - Check running studios by name before create to avoid quota waste.  
   - Expose via same module for reuse in training orchestration.

---

### Implementation (merged + hardened)

```bash
# /opt/axentx/vanguard/backend/manifest.py
import json
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

try:
    from huggingface_hub import HfApi, list_repo_tree
except Exception:  # pragma: no cover - graceful fallback
    HfApi = None
    list_repo_tree = None

HF_API = HfApi() if HfApi else None

# -------------------------
# Deterministic routing
# -------------------------
def deterministic_repo_index(slug: str, n_siblings: int = 5) -> int:
    """Map slug -> sibling index for commit-cap spreading (128/hr/repo)."""
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % max(1, n_siblings)

def sibling_repo_name(base_repo: str, idx: int) -> str:
    """Given org/repo return org/repo-sibling-<idx> (convention)."""
    org, name = base_repo.split("/", 1)
    return f"{org}/{name}-sibling-{idx}"

# -------------------------
# Manifest generation
# -------------------------
def _normalize_sha256(node) -> Optional[str]:
    # Prefer LFS OID; fallback to node.sha if available; else None
    lfs = getattr(node, "lfs", None)
    if isinstance(lfs, dict) and lfs.get("oid"):
        oid = lfs["oid"]
        if oid.startswith("sha256:"):
            return oid.split(":", 1)[1]
        return oid
    if hasattr(node, "sha") and node.sha:
        return node.sha
    return None

def build_manifest(
    repo: str,
    date_folder: str,
    out_dir: Optional[str] = None,
    recursive: bool = True
) -> Path:
    """
    Single API call: list_repo_tree for date_folder.
    Produces manifest-{date}.json with [{path, sha256, size}].
    """
    if HF_API is None or list_repo_tree is None:
        raise RuntimeError("huggingface_hub not available; cannot list repo tree.")

    tree = list_repo_tree(repo=repo, path=date_folder, recursive=recursive)
    entries: List[Dict] = []
    for node in tree:
        if getattr(node, "type", None) != "file":
            continue
        entries.append({
            "path": node.path,
            "sha256": _normalize_sha256(node) or "",
            "size": int(getattr(node, "size", 0))
        })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_files": len(entries),
        "entries": entries
    }

    out_dir = Path(out_dir or os.getcwd())
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"manifest-{date_folder}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    return out_path

def build_local_mirror_manifest(
    mirror_root: str,
    date_folder: str,
    out_path: Optional[str] = None
) -> Path:
    """
    For local mirror at batches/mirror-merged/{date}/ produce manifest.json
    with local {path, sha256, size}. Paths in manifest are relative to mirror_root.
    """
    root = Path(mirror_root).expanduser().resolve()
    date_path = root / date_folder
    if not date_path.is_dir():
        raise NotADirectoryError(f"Date folder not found: {date_path}")

    entries: List[Dict] = []
    for fpath in date_path.rglob("*"):
        if not fpath.is_file():
            continue
        rel = fpath.relative_to(root)
        sha = hashlib.sha256(fpath.read_bytes()).hexdigest()
        entries.append({
            "path": str(rel).replace("\\", "/"),
            "sha256": sha,
            "size": fpath.stat().st_size
        })

    manifest = {
        "mirror_root": str(root),
        "date_folder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_files": len(entries),
        "entries": entries
    }

    out = Path(out_path) if out_path else (root / date_folder / "manifest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))
    return out

# -------------------------
# CDN + integrity helpers
# -------------------------
def cdn_url(repo: str, path: str) -> str:
    """CDN bypass URL (no Authorization header)."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def validate_local_against_manifest(manifest_path: str, root: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """
    Returns (ok_paths, failed_paths).
    For each entry, if local file exists and sha256 matches -> ok; else failed.
    """
    manifest = json.loads(Path(manifest_path).read_text())
    root = Path(root or os.getcwd())
    ok, failed = [], []
    for e in manifest.get("entries", []):
        p = root / e["path"]
        expected = e.get("sha256")
        if not p.is_file():
            failed.append(e["path"])
            continue
        if expected:
            actual = hashlib.sha256(p.read_bytes()).hexdigest()
            if actual != expected:
                failed.append(e["path"])
                continue
        ok.append(e["path"])
    return ok, failed

def stream_cdn_with_retry(manifest_path: str, max_retries: int = 3, timeout: int = 30) -> None:
    """
    Example generator-style helper: yields (path, url) for CDN streaming.
    Includes simple retry/backoff and integrity check when possible.
    """
    manifest = json.loads(Path(manifest_path).read_text())
    import requests
    for e in manifest.get("entries", []):
        url = cdn_url(manifest["repo"], e["path"])
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.get(url, timeout=timeout, stream=True)
                resp.raise_for_status()
               
