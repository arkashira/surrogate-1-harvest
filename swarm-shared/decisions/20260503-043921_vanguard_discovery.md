# vanguard / discovery

## Final Synthesized Implementation (single, actionable plan)

**Core problem**: training pipelines rely on runtime `list_repo_tree` calls, causing HF API 429s, non-deterministic file lists, and no integrity verification for CDN downloads.

**Resolution**: add a deterministic, content-addressed snapshot workflow that is generated once per date folder and embedded in training jobs so they use CDN-only fetches with zero API calls and verified integrity.

---

### 1. Single utility: `/opt/axentx/vanguard/bin/make-snapshot.py`

Combines the strongest parts of both proposals:
- Supports **local folder** (full sha256) and **HF repo** (size-only, optional spot-check sha256 via downloads).
- Produces a strict, reproducible `snapshot.json` that training can consume.
- Lightweight and CI-friendly.

```python
#!/usr/bin/env python3
"""
make-snapshot.py
Produce content-addressed snapshot.json for a date folder.

Usage:
  # Local folder (full verification)
  ./make-snapshot.py --local ./data/2026-04-29 --repo datasets/org/repo --date 2026-04-29 --out snapshot.json

  # HF repo (size-only snapshot; optional spot-check sha256 via --spot-check N)
  ./make-snapshot.py --hf --repo datasets/org/repo --date 2026-04-29 --out snapshot.json --spot-check 5
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

try:
    from huggingface_hub import hf_hub_download, list_repo_tree
except ImportError:
    hf_hub_download = None  # type: ignore
    list_repo_tree = None  # type: ignore

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def build_local_snapshot(root: Path, repo: str, date: str) -> Dict:
    root = root.resolve()
    files: List[Dict] = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(root)).replace("\\", "/")
            files.append(
                {
                    "path": rel,
                    "sha256": sha256_file(p),
                    "size": p.stat().st_size,
                }
            )
    return {
        "date": date,
        "repo": repo,
        "generated_at": utcnow_iso(),
        "mode": "local",
        "files": files,
    }

def build_hf_snapshot(repo: str, date_folder: str, spot_check: int = 0) -> Dict:
    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub not installed; cannot query HF repo.")

    # Avoid recursive=True to reduce request size and 429 risk.
    top = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files: List[Dict] = []
    for e in top:
        if e.type == "file":
            entry_path = f"{date_folder}/{e.path}" if not e.path.startswith(date_folder) else e.path
            files.append(
                {
                    "path": entry_path,
                    "sha256": None,
                    "size": e.size,
                }
            )

    # Optional spot-check: download a few files to compute sha256.
    if spot_check > 0 and hf_hub_download is not None:
        import random
        candidates = [f for f in files if f["sha256"] is None]
        selected = random.sample(candidates, min(spot_check, len(candidates))) if candidates else []
        for f in selected:
            try:
                local_path = hf_hub_download(repo_id=repo, filename=f["path"], repo_type="dataset")
                f["sha256"] = sha256_file(Path(local_path))
                f["spot_checked"] = True
                # Small delay to reduce burst risk
                time.sleep(0.2)
            except Exception:
                # If spot-check fails, keep sha256=None but continue.
                f["spot_checked"] = False

    return {
        "date": date_folder,
        "repo": repo,
        "generated_at": utcnow_iso(),
        "mode": "hf",
        "files": files,
    }

def main() -> int:
    parser = argparse.ArgumentParser(description="Create content-addressed snapshot.json")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--local", type=Path, help="Local folder containing date-partitioned files")
    group.add_argument("--hf", action="store_true", help="Build snapshot from HF repo (size-only by default)")

    parser.add_argument("--repo", help="HF repo (e.g., datasets/username/repo)")
    parser.add_argument("--date", help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default="snapshot.json", help="Output path")
    parser.add_argument("--spot-check", type=int, default=0, help="For --hf: compute sha256 for N random files")
    args = parser.parse_args()

    if args.local:
        if not args.local.is_dir():
            print(f"Error: {args.local} is not a directory", file=sys.stderr)
            return 1
        date = args.date or args.local.name
        repo = args.repo or "local"
        snapshot = build_local_snapshot(args.local, repo, date)
    else:
        if not args.repo or not args.date:
            print("Error: --hf requires --repo and --date", file=sys.stderr)
            return 1
        snapshot = build_hf_snapshot(args.repo, args.date, spot_check=args.spot_check)

    out = Path(args.out)
    out.write_text(json.dumps(snapshot, indent=2))
    print(f"Snapshot written to {out} ({len(snapshot['files'])} files)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/bin/make-snapshot.py
```

---

### 2. Optional companion: `/opt/axentx/vanguard/bin/verify-snapshot.py`

Lightweight verifier for CI/cron:

```python
#!/usr/bin/env python3
"""
verify-snapshot.py
Verify local files against snapshot.json (sha256/size).

Usage:
  ./verify-snapshot.py --snapshot snapshot.json --root ./data/2026-04-29
  ./verify-snapshot.py --snapshot snapshot.json --root ./data/2026-04-29 --strict-sha
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--strict-sha", action="store_true", help="Require sha256 for all entries")
    args = parser.parse_args()

    snap = json.loads(args.snapshot.read_text())
    root = args.root.resolve()
    errors = []
    for f in snap.get("files", []):
        p = (root / f["path"]).resolve()
        if not p.exists():
            errors.append(f"MISSING: {f['path']}")
            continue
        if p.stat().st_size != f["size"]:
            errors.append(f"SIZE MISMATCH: {f['path']} expected={f['size']} actual={p.stat().st_size}")
        if f.get("sha256"):
