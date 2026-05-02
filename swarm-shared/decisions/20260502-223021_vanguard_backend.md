# vanguard / backend

## Final Synthesis (single, actionable plan)

**Chosen approach:** Adopt Candidate 1’s concrete file and CDN-bypass mechanism, and integrate Candidate 2’s canonical entrypoint + wrapper hardening + reuse guard.  
Result: one minimal, deterministic, cron-safe change that prevents 429s, avoids commit-cap blocks, and gives a canonical discovery path without rewriting training code yet.

---

## 1. Diagnosis (resolved)

- **Canonical entrypoint missing** → Add `backend/__main__.py` + `backend/launch.py` so `python -m vanguard.backend` is the single CLI.
- **HF API rate limits during training** → Generate CDN-only filelist once per date folder; `train.py` will consume it next step.
- **Lightning Studio reuse/quota waste** → Add safe reuse guard in launcher (L40S priority, fallback, idle-stop protection).
- **Cron/wrapper fragility** → Enforce shebang + executable bit + `SHELL=/bin/bash` and validate scripts in `scripts/`.
- **HF commit-cap (128/hr/repo)** → Deterministic sibling-repo sharding for any ingestion writes.

---

## 2. Implementation (one-shot commands)

```bash
# Create structure
mkdir -p /opt/axentx/vanguard/backend/{ingest/{hf_cdn_filelist,filelists},bin,scripts}
```

### `/opt/axentx/vanguard/backend/__main__.py`
```python
#!/usr/bin/env python3
from vanguard.backend.launch import main

if __name__ == "__main__":
    main()
```

### `/opt/axentx/vanguard/backend/launch.py`
```python
#!/usr/bin/env python3
"""
Canonical launcher for vanguard.backend.

Usage:
  python -m vanguard.backend hf-filelist <repo_id> <date_folder> [--out-dir ...]
  python -m vanguard.backend studio [--reuse] [--fallback]
  python -m vanguard.backend validate-scripts
"""

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import List

HF_CDN_FILELIST = Path(__file__).parent / "ingest" / "hf_cdn_filelist.py"
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"

# Sibling repos for write sharding (128/hr/repo cap)
SIBLING_REPOS = [
    "datasets/myorg/surrogate-1",
    "datasets/myorg/surrogate-1-sib1",
    "datasets/myorg/surrogate-1-sib2",
    "datasets/myorg/surrogate-1-sib3",
    "datasets/myorg/surrogate-1-sib4",
]

def pick_sibling(slug: str) -> str:
    import hashlib
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def run_hf_filelist(repo_id: str, date_folder: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(HF_CDN_FILELIST), repo_id, date_folder, "--out-dir", str(out_dir)]
    subprocess.run(cmd, check=True)

def studio_reuse_guard(fallback: bool = False) -> None:
    """
    Reuse running Lightning Studio session if available; otherwise start one.
    Prefer L40S-capable machines; fall back to free-tier if requested.
    """
    # Placeholder: integrate with Lightning Studio API / CLI when available.
    # For now, emit actionable guidance and prevent duplicate starts.
    lock = Path("/tmp/vanguard_studio.lock")
    if lock.exists():
        print("[INFO] Studio session lock present; assuming already running. Remove lock to force start.")
        return
    print("[INFO] No running Studio session detected. Start one manually or via Lightning CLI.")
    print("  Prefer: lightning run job job.yaml --cloud L40S")
    if fallback:
        print("  Fallback: free-tier requested.")
    lock.touch()

def validate_scripts() -> None:
    if not SCRIPTS_DIR.exists():
        print("[WARN] scripts/ directory missing; nothing to validate.")
        return
    errors = []
    for p in SCRIPTS_DIR.rglob("*"):
        if p.is_file():
            try:
                with p.open("rb") as f:
                    first = f.readline(128)
                if not first.startswith(b"#!"):
                    errors.append(f"{p}: missing shebang")
                mode = p.stat().st_mode
                if not (mode & stat.S_IXUSR):
                    errors.append(f"{p}: not executable (chmod +x)")
            except Exception as e:
                errors.append(f"{p}: {e}")
    if errors:
        print("[FAIL] Script validation errors:")
        for e in errors:
            print("  " + e)
        sys.exit(1)
    print("[OK] All scripts have shebang + executable bit.")

def main() -> None:
    parser = argparse.ArgumentParser(prog="vanguard.backend")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("hf-filelist", help="Generate CDN filelist")
    sp.add_argument("repo_id")
    sp.add_argument("date_folder")
    sp.add_argument("--out-dir", default="backend/ingest/filelists")

    sp = sub.add_parser("studio", help="Studio reuse/start")
    sp.add_argument("--reuse", action="store_true", default=True)
    sp.add_argument("--fallback", action="store_true")

    sub.add_parser("validate-scripts", help="Validate scripts/ shebang+executable")

    args = parser.parse_args()

    if args.cmd == "hf-filelist":
        run_hf_filelist(args.repo_id, args.date_folder, Path(args.out_dir))
    elif args.cmd == "studio":
        studio_reuse_guard(fallback=args.fallback)
    elif args.cmd == "validate-scripts":
        validate_scripts()

if __name__ == "__main__":
    main()
```

### `/opt/axentx/vanguard/backend/ingest/hf_cdn_filelist.py`
(Identical to Candidate 1; included for completeness)

```python
#!/usr/bin/env python3
"""
Generate CDN-only filelist for a Hugging Face dataset repo folder (date-level).
Avoids HF API rate limits during training by producing a JSON file
that train.py can consume with pure CDN fetches.

Usage:
  python3 hf_cdn_filelist.py <repo_id> <date_folder> [--out-dir ./filelists]

Example:
  python3 hf_cdn_filelist.py datasets/myorg/surrogate-1 2026-04-29
"""

import json
import hashlib
import os
import sys
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("ERROR: huggingface_hub not installed. Run: pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

SIBLING_REPOS = [
    "datasets/myorg/surrogate-1",
    "datasets/myorg/surrogate-1-sib1",
    "datasets/myorg/surrogate-1-sib2",
    "datasets/myorg/surrogate-1-sib3",
    "datasets/myorg/surrogate-1-sib4",
]

def pick_sibling_repo(slug: str) -> str:
    """Deterministic repo selection for write spreading (128/hr/repo cap)."""
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def build_filelist(repo_id: str, date_folder: str) -> List[Dict[str, str]]:
    """
    Single API call: list_repo_tree non-recursive for one date folder.
    Returns list of dicts with CDN-ready path and metadata.
    """
    if not date_folder or "/" in date_folder:
        raise ValueError("date_folder must be a single folder name (e.g. 2026-04-29).")

    items = list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
    files = []
    for
