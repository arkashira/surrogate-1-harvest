# vanguard / quality

## Final Synthesized Implementation

### 1. Diagnosis (merged, de-duplicated, prioritized)
- **No persisted file manifest**: backend recomputes repo listings on every training run → HF API 429 risk and wasted quota.
- **Authenticated HF API during training**: path-based loading adds auth overhead and rate-limit exposure; should use CDN-only URLs.
- **No durable cache layer**: manifests are ephemeral; backend should store date-keyed JSON snapshots.
- **Lightning Studio idle/timeout handling**: no auto-restart or reuse logic for stopped/idle studios.
- **Orchestration/compute coupling**: risk of local model loading on Mac instead of delegating to Lightning/Kaggle/Cerebras.

### 2. Single Proposed Change
Add a small, import-safe manifest/cache layer and make training use CDN-only URLs; add a lightweight Lightning Studio reuse wrapper; keep orchestration separate from compute.

### 3. Implementation (merged, corrected, actionable)

```bash
# 1) Manifest module (single source of truth, CDN-first, import-safe)
mkdir -p /opt/axentx/vanguard/backend/manifests
cat > /opt/axentx/vanguard/backend/manifest.py << 'PY'
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

try:
    from huggingface_hub import list_repo_tree, HfApi
    HF_AVAILABLE = True
except Exception:
    HF_AVAILABLE = False
    list_repo_tree = None  # type: ignore
    HfApi = None  # type: ignore

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _date_tag() -> str:
    return datetime.utcnow().strftime("%Y%m%d")

def _manifest_fname(repo_id: str, folder_path: str, date_tag: str) -> Path:
    slug = folder_path.strip("/").replace("/", "_") or "root"
    safe_repo = repo_id.replace("/", "_")
    return MANIFEST_DIR / f"{safe_repo}_{slug}_{date_tag}.json"

def build_manifest(
    repo_id: str,
    folder_path: str,
    output_path: Optional[Path] = None,
    max_retries: int = 3,
    backoff: int = 360,
) -> Dict:
    """
    Single authenticated API call to list one folder (non-recursive).
    Returns manifest and writes JSON to manifests/ folder.
    Prefer CDN URLs for data loading.
    """
    if not HF_AVAILABLE or list_repo_tree is None or HfApi is None:
        raise RuntimeError("huggingface_hub not available; cannot build manifest.")

    api = HfApi()
    attempt = 0
    while attempt < max_retries:
        try:
            items = list_repo_tree(repo_id=repo_id, path=folder_path, recursive=False)
            break
        except Exception:
            attempt += 1
            if attempt >= max_retries:
                raise
            time.sleep(backoff)

    files = []
    for item in items:
        if getattr(item, "type", None) == "file":
            # CDN URL (no auth required for public datasets/files)
            cdn_url = (
                f"https://huggingface.co/datasets/{repo_id}/resolve/main/"
                f"{item.path.lstrip('/')}"
            )
            files.append({
                "path": item.path,
                "size": getattr(item, "size", None),
                "cdn_url": cdn_url,
            })

    manifest = {
        "repo_id": repo_id,
        "folder_path": folder_path,
        "created_at": _now_iso(),
        "files": files,
        "file_count": len(files),
    }

    if output_path is None:
        output_path = _manifest_fname(repo_id, folder_path, _date_tag())
    else:
        output_path = Path(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return manifest

def load_manifest(repo_id: str, folder_path: str, date_tag: Optional[str] = None) -> Optional[Dict]:
    if date_tag is None:
        date_tag = _date_tag()
    p = _manifest_fname(repo_id, folder_path, date_tag)
    if not p.is_file():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def get_or_build_manifest(repo_id: str, folder_path: str, max_age_hours: int = 24) -> Dict:
    """
    Return cached manifest if fresh; otherwise rebuild.
    """
    existing = load_manifest(repo_id, folder_path)
    if existing:
        created = existing.get("created_at")
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                if 0 <= age <= max_age_hours:
                    return existing
            except Exception:
                pass
    return build_manifest(repo_id, folder_path)
PY

# 2) Lightning Studio reuse wrapper (import-safe, minimal)
cat > /opt/axentx/vanguard/backend/lightning_utils.py << 'PY'
import time
from typing import Optional

try:
    from lightning import Studio, Machine, Teamspace
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False
    Studio = None  # type: ignore
    Machine = None  # type: ignore
    Teamspace = None  # type: ignore

def get_or_start_studio(
    name: str,
    machine: str = "L40S",
    idle_timeout_minutes: int = 30,
    max_retries: int = 3,
) -> Optional[object]:
    """
    Reuse running studio or start a new one.
    Returns Studio handle or None if unavailable.
    """
    if not LIGHTNING_AVAILABLE or Studio is None or Machine is None or Teamspace is None:
        return None

    for attempt in range(max_retries):
        try:
            studios = Teamspace.studios()
            for s in studios:
                if s.name == name:
                    if s.status == "running":
                        return s
                    if s.status in ("stopped", "idle"):
                        target_machine = getattr(Machine, machine, Machine.L40S)
                        s.start(machine=target_machine)
                        for _ in range(10):
                            s.refresh()
                            if s.status == "running":
                                return s
                            time.sleep(6)
                        return s
            # not found: create
            studio = Studio(name=name, machine=machine, create_ok=True)
            return studio
        except Exception:
            if attempt == max_retries - 1:
                raise
            time.sleep(15)
    return None
PY

# 3) Training launcher (CDN-first, manifest-driven, import-safe)
TRAIN_PATH="/opt/axentx/vanguard/backend/train.py"
mkdir -p "$(dirname "$TRAIN_PATH")"
cat > "$TRAIN_PATH" << 'PY'
import json
import os
from pathlib import Path
from typing import Iterator, Dict

# Manifest + CDN-first data loading for training (avoids HF API during compute).
# Intended to run on Lightning Studio / remote compute.

MANIFEST_DIR = Path(__file__).parent / "manifests"

def build_cdn_file_list(manifest_path: Path) -> list[str]:
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        m = json.load(f)
    return [item["cdn_url"] for item in m.get("files", []) if "cdn_url" in item]

def dummy_data_iter(cdn_urls: list[str]) -> Iterator[Dict[str, str]]:
    # Replace with real streaming loader (e.g., parquet/http/webdataset).
    for u in cdn_urls:
        yield {"url": u, "prompt": "", "response": ""}

def main() -> None
