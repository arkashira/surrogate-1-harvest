# airship / discovery

## Final Synthesized Implementation (≤2h)

**Goal**: Harden `airship discover` into a deterministic, CDN-only orchestrator that eliminates HF API rate limits and PyArrow schema errors while producing reproducible file lists and safe ingestion artifacts.

**Core Principles** (resolve contradictions in favor of correctness + actionability):
- **Single source of truth**: One orchestration module, not two competing implementations.
- **Cache-first determinism**: Always prefer cached file lists (<24h) to avoid 429s; API call only once per day per folder.
- **Strict schema projection**: Drop all non-`{prompt,response}` columns at parse time to prevent CastError.
- **Cron-safe by default**: Shebang, executable bit, `SHELL=/bin/bash`, absolute paths, and `set -euo pipefail`.
- **No new dependencies**: `requests`, `json`, `hashlib`, `os`, `pathlib` only.

---

## Implementation Plan (Actionable Steps)

1. **Locate entrypoint**  
   Confirm `airship/cli.py` contains the `airship discover` command. If split into `airship/cli/discover.py`, consolidate or patch accordingly.

2. **Create CDN-only discovery module** (`airship/cdnsafe_discover.py`)  
   Responsibilities:
   - One-time HF API call (after rate-limit window) to `list_repo_tree(path, recursive=False)` for a single date folder.
   - Persist file list to JSON (deterministic, sorted).
   - Generate ingestion manifest with CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`).
   - Project schema to `{prompt, response}` only at parse time; drop all other fields.
   - Filename attribution via `batches/mirror-merged/{date}/{slug}.parquet` (no `source`/`ts` cols).

3. **Update `airship discover` command**  
   - Accept `--date-folder` and `--repo` (default: datasets repo used by surrogate-1).
   - If cached file list exists and is <24h old, reuse (skip API call).
   - Otherwise, run CDN-safe discovery and emit `filelist.json` + `manifest.json`.
   - Exit 0 with paths for downstream ingestion.

4. **Add cron-safe invocation guard**  
   - Shebang `#!/usr/bin/env bash`.
   - Ensure executable bit (`chmod +x`).
   - Set `SHELL=/bin/bash` in crontab (if used).
   - Use absolute paths in cron jobs.

5. **Smoke test**  
   - Run against a small public dataset (e.g., `openai/summarize_from_feedback`).
   - Verify no API calls during CDN fetch phase.
   - Confirm parquet projection works (read one file, check columns).

---

## Code Snippets

### `airship/cdnsafe_discover.py`
```python
#!/usr/bin/env python3
"""
CDN-safe discovery for HF datasets.
- Single API call to list files in a date folder.
- Persists deterministic file list.
- Generates CDN-only manifest for ingestion.
- Projects schema to {prompt, response} at parse time.
"""
import json
import os
import hashlib
import datetime
from pathlib import Path
from typing import List, Dict

import requests

HF_API_BASE = "https://huggingface.co/api"
HF_CDN_BASE = "https://huggingface.co/datasets"

# Rate-limit tolerance: 1000 req/5min for API; CDN is unrestricted.
API_RETRY_WAIT = 360  # seconds after 429


def list_date_folder(repo: str, date_folder: str, token: str = None) -> List[str]:
    """
    List files in a single folder (non-recursive) using HF API.
    Returns sorted list of relative paths.
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{HF_API_BASE}/datasets/{repo}/tree/{date_folder}"
    params = {"recursive": "false"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code == 429:
        wait = int(resp.headers.get("retry-after", API_RETRY_WAIT))
        raise RuntimeError(f"HF API 429; wait {wait}s")
    resp.raise_for_status()

    entries = resp.json()
    files = sorted(e["path"] for e in entries if e["type"] == "file")
    return files


def slug_from_path(path: str) -> str:
    """
    Deterministic slug for attribution filename.
    Example: batches/mirror-merged/2026-04-29/abc123.parquet
    """
    h = hashlib.sha256(path.encode()).hexdigest()[:12]
    return h


def build_manifest(repo: str, files: List[str], date_folder: str) -> Dict:
    """
    Build ingestion manifest with CDN URLs and projection plan.
    """
    base = f"{HF_CDN_BASE}/{repo}/resolve/main"
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "files": [],
    }

    for f in files:
        cdn_url = f"{base}/{f}"
        slug = slug_from_path(f)
        out_name = f"batches/mirror-merged/{date_folder}/{slug}.parquet"
        manifest["files"].append(
            {
                "source_path": f,
                "cdn_url": cdn_url,
                "out_name": out_name,
                # Projection rule: keep only prompt/response at parse time
                "projection": {"keep": ["prompt", "response"], "drop_all_other": True},
            }
        )
    return manifest


def discover(repo: str, date_folder: str, out_dir: Path, token: str = None) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cache_file = out_dir / "filelist.json"
    manifest_file = out_dir / "manifest.json"

    # Reuse cache if fresh (<24h)
    use_cache = False
    if cache_file.exists() and manifest_file.exists():
        age = datetime.datetime.utcnow() - datetime.datetime.utcfromtimestamp(
            cache_file.stat().st_mtime
        )
        if age.total_seconds() < 86400:
            use_cache = True

    if use_cache:
        files = json.loads(cache_file.read_text())
    else:
        files = list_date_folder(repo, date_folder, token=token)
        cache_file.write_text(json.dumps(files, indent=2))

    manifest = build_manifest(repo, files, date_folder)
    manifest_file.write_text(json.dumps(manifest, indent=2))
    return manifest_file


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CDN-safe HF dataset discovery")
    parser.add_argument("--repo", default="openai/summarize_from_feedback", help="HF dataset repo")
    parser.add_argument("--date-folder", default="2026-04-29", help="Folder in dataset (e.g. 2026-04-29)")
    parser.add_argument("--out-dir", default="discovery_out", help="Output directory")
    parser.add_argument("--token", default=None, help="HF token (optional; not used for CDN downloads)")
    args = parser.parse_args()

    mf = discover(args.repo, args.date_folder, Path(args.out_dir), token=args.token)
    print(f"Manifest written: {mf}")
```

### Update `airship/cli.py` (or equivalent) snippet
```python
# In your click/typer command group
import subprocess
from pathlib import Path

@cli.command("discover")
@click.option("--date-folder", default="2026-04-29")
@click.option("--repo", default="openai/summarize_from_feedback")
@click.option("--out-dir", default="discovery_out")
def discover_cmd(date_folder, repo, out_dir):
    """CDN-safe discovery: list once, fetch via CDN, project schema."""
    script = Path(__file__).parent / "cdnsafe_discover.py"
    subprocess.run(
        ["python3", str(script), "--repo", repo, "--date-folder", date_folder, "--
