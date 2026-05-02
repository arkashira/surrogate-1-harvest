# vanguard / backend

## Final Synthesized Design (Best of Both Candidates)

**Core principle**: One canonical, discoverable backend entrypoint that fixes rate-limit, quota-burn, and cron-execution failures while remaining strictly additive and idempotent.

---

### 1. Diagnosis (resolved)
- **Canonical entrypoint**: Add `/opt/axentx/vanguard/backend/__main__.py` + `pyproject.toml` console script `vanguard-backend` for CLI/`cron`/`systemd` parity.
- **HF CDN bypass**: Single `list_repo_tree` call → `manifest-{date}.json` + CDN URL list to avoid 429 during surrogate-1 training.
- **Lightning Studio reuse**: List running studios and reuse by name; never auto-create without explicit intent to avoid quota burn.
- **Wrapper hygiene**: Idempotent shebang + `chmod +x` fix for `opus-pr-reviewer`/`active-learning` cron jobs.
- **Config/env guard**: Centralized `_check_env()` for required tools (HF token, Lightning account) and deterministic defaults.

---

### 2. Implementation

```python
# /opt/axentx/vanguard/backend/orchestrate.py
import json
import os
import hashlib
import sys
from pathlib import Path
from typing import List, Optional, Any, Dict

# ---- optional deps ----
try:
    from huggingface_hub import list_repo_tree, HfApi
    HF_AVAILABLE = True
except Exception:
    HF_AVAILABLE = False

try:
    import lightning as L
    from lightning.fabric.helpers import _get_teamspace
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False


# ---- helpers ----
def _require(cond: bool, name: str) -> None:
    if not cond:
        raise RuntimeError(f"{name} not available. Install required extras.")


def _check_env() -> Dict[str, str]:
    """Centralized env/config validation for backend services."""
    hf_token = os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")
    if not hf_token:
        print("WARN: HF_TOKEN not set; some HF operations may fail.", file=sys.stderr)
    return {"HF_TOKEN": hf_token or ""}


# ---- core ops ----
def build_hf_file_manifest(repo: str, date_folder: str, out_dir: Optional[str] = None) -> str:
    """
    Single API call to list files in date_folder (non-recursive) and produce
    manifest JSON for CDN-only training.
    """
    _require(HF_AVAILABLE, "huggingface_hub")
    tree = list_repo_tree(repo=repo, path=date_folder.rstrip("/"), recursive=False)
    files = sorted(f.rfilename for f in tree if f.type == "file")

    out_dir = out_dir or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    label = os.path.basename(date_folder.rstrip("/")) or "latest"
    out_path = os.path.join(out_dir, f"manifest-{label}.json")

    manifest = {
        "repo": repo,
        "folder": date_folder.rstrip("/"),
        "files": files,
        "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder.rstrip('/')}",
    }

    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return out_path


def pick_sibling_repo(slug: str, n: int = 5) -> str:
    """
    Deterministic sibling repo selection to bypass HF 128/hr/repo commit cap.
    Returns repo name (e.g., org/dataset-sibling2).
    """
    parts = slug.split("/")
    if len(parts) != 2:
        raise ValueError("slug must be 'owner/repo'")
    owner, repo = parts
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % n
    if idx == 0:
        return slug
    return f"{owner}/{repo}-sibling{idx}"


def reuse_or_create_studio(name: str, machine: str = "L40S", project: str = "vanguard") -> Optional[Any]:
    """
    Reuse a running Lightning Studio when possible to save quota.
    Returns studio object if reused; None otherwise (Lightning unavailable or not running).
    Caller must explicitly create if None and intent is to start.
    """
    _require(LIGHTNING_AVAILABLE, "lightning")
    try:
        teamspace = _get_teamspace()
        for s in teamspace.studios:
            if getattr(s, "name", None) == name and getattr(s, "status", None) == "Running":
                print(f"Reusing running studio: {name}")
                return s
        print(f"No running studio '{name}' found (machine={machine}). Create explicitly if desired.")
        return None
    except Exception as e:
        print(f"Studio reuse check failed: {e}")
        return None


def ensure_bash_wrapper(path: str) -> bool:
    """
    Idempotent wrapper fix: ensure shebang and executable bit.
    """
    p = Path(path)
    if not p.is_file():
        return False

    content = p.read_text()
    if not content.startswith("#!"):
        content = "#!/usr/bin/env bash\n" + content
        p.write_text(content)

    current_mode = p.stat().st_mode
    if not (current_mode & 0o111):
        p.chmod(current_mode | 0o111)
    return True


def build_cdn_urls(manifest_path: str) -> List[str]:
    with open(manifest_path) as f:
        m = json.load(f)
    prefix = m["cdn_prefix"].rstrip("/")
    return [f"{prefix}/{f}" for f in m["files"]]
```

```python
# /opt/axentx/vanguard/backend/__main__.py
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend.orchestrate import (
    _check_env,
    build_hf_file_manifest,
    pick_sibling_repo,
    reuse_or_create_studio,
    ensure_bash_wrapper,
    build_cdn_urls,
)


def main() -> None:
    _check_env()
    parser = argparse.ArgumentParser(description="Vanguard backend orchestration")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_mf = sub.add_parser("manifest", help="Build HF file manifest for CDN training")
    p_mf.add_argument("--repo", required=True)
    p_mf.add_argument("--folder", required=True)
    p_mf.add_argument("--out-dir", default=".")

    p_sib = sub.add_parser("sibling", help="Pick sibling repo for slug")
    p_sib.add_argument("--slug", required=True)
    p_sib.add_argument("--n", type=int, default=5)

    p_studio = sub.add_parser("studio", help="Reuse running studio (safe)")
    p_studio.add_argument("--name", required=True)
    p_studio.add_argument("--machine", default="L40S")

    p_wrap = sub.add_parser("wrapper", help="Ensure bash wrapper")
    p_wrap.add_argument("--path", required=True)

    p_urls = sub.add_parser("cdn-urls", help="Print CDN URLs from manifest")
    p_urls.add_argument("--manifest", required=True)

    args = parser.parse_args()

    if args.cmd == "manifest":
        out = build_hf_file_manifest(args.repo, args.folder, args.out_dir)
        print(out)
    elif args.cmd == "sibling":
        print(pick_sibling_repo(args.slug, args.n))
    elif args.cmd == "studio":
        reuse_or_create_studio(args.name, args.machine)
    elif args.cmd == "wrapper":
        ok = ensure_bash_wrapper(args.path)
        sys.exit(0 if ok else 1)
    elif args.cmd == "cdn-urls":
        for u in build_cdn_urls(args.manifest):
            print(u)


if __name__ == "__main__":
    main()
```

```toml
# /opt/axentx/vanguard/pyproject.toml (excerpt)
[project.scripts]
vanguard-backend = "vanguard.backend.__main__:main"
```

---

### 3
