# vanguard / discovery

## Final Consolidated Implementation

### Diagnosis (merged, de-duplicated)
- **No persisted manifest**: every training run calls authenticated `list_repo_tree`, burning HF API quota and risking 429s.
- **Mixed-schema failures**: `load_dataset(streaming=True)` or per-file loads encounter `pyarrow.CastError`/schema mismatches in heterogeneous repos.
- **API-heavy fetches**: training uses authenticated API calls instead of public CDN URLs (`resolve/main/...`), wasting quota and increasing failure surface.
- **HF commit cap exposure**: no deterministic sibling-repo assignment for writes; risk of hitting 128 commits/hour limit.
- **Lightning Studio waste**: no reuse guard; runs recreate studios and lose work when idle-stop kills training without restart logic.

### Single Proposed Change
Add a lightweight discovery+manifest utility in `/opt/axentx/vanguard/discovery.py` that:
- Runs once per `(repo, dateFolder)` after rate-limit window clears.
- Calls `list_repo_tree(recursive=False)` per folder and persists `file-list.json` with CDN URLs and deterministic sibling-repo assignment.
- Exposes a CDN-only dataset reader and a Studio reuse guard to eliminate HF API calls during training and prevent quota waste.

### Implementation (single file + snippets)

```bash
# /opt/axentx/vanguard/discovery.py
#!/usr/bin/env python3
"""
Discovery utility for vanguard.
Produces (repo, dateFolder) -> file-list manifest with CDN URLs
and deterministic HF sibling-repo assignment to avoid commit caps.
"""
import json
import hashlib
import os
from pathlib import Path
from typing import Dict, List, Optional

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    list_repo_tree = None  # type: ignore


SIBLING_REPOS = [
    "axentx/vanguard-mirror-0",
    "axentx/vanguard-mirror-1",
    "axentx/vanguard-mirror-2",
    "axentx/vanguard-mirror-3",
    "axentx/vanguard-mirror-4",
]


def cdn_url(repo: str, path: str, revision: str = "main") -> str:
    """Public CDN URL (no auth, bypasses API rate limits)."""
    # Normalize repo to dataset-style CDN path
    return f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{path}"


def pick_sibling(slug: str) -> str:
    """Deterministic sibling repo by hash to spread writes and avoid 128/hr cap."""
    digest = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(digest, 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]


def build_manifest(
    repo: str,
    date_folder: str,
    out_path: Optional[str] = None,
    revision: str = "main",
) -> Dict:
    """
    Build manifest for repo/date_folder.
    Uses list_repo_tree per folder (non-recursive) to minimize API calls.
    Returns:
      {
        "repo": repo,
        "date_folder": date_folder,
        "revision": revision,
        "files": [{"path": str, "cdn_url": str, "size": int|None}],
        "sibling": chosen_sibling_for_writes
      }
    """
    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub not installed")

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "revision": revision,
        "files": [],
        "sibling": pick_sibling(f"{repo}/{date_folder}"),
    }

    # List top-level of date_folder (non-recursive). If deeper structure exists,
    # downstream training should enumerate known subpaths or use a bounded crawl.
    try:
        tree = list_repo_tree(repo=repo, path=date_folder, recursive=False, revision=revision)
    except Exception as exc:
        raise RuntimeError(f"Failed to list repo tree for {repo}/{date_folder}: {exc}") from exc

    for entry in tree:
        if entry.type != "file":
            continue
        rel = entry.path
        manifest["files"].append(
            {
                "path": rel,
                "cdn_url": cdn_url(repo, rel, revision=revision),
                "size": getattr(entry, "size", None),
            }
        )

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

    return manifest


def load_manifest(manifest_path: str) -> Dict:
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build HF file manifest for CDN-only training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., axentx/some-data)")
    parser.add_argument("--date-folder", required=True, help="Date folder inside repo (e.g., 2026-05-03)")
    parser.add_argument("--out", default="file-list.json", help="Output manifest path")
    parser.add_argument("--revision", default="main", help="Git revision")
    args = parser.parse_args()

    m = build_manifest(args.repo, args.date_folder, out_path=args.out, revision=args.revision)
    print(f"Wrote {len(m['files'])} files to {args.out}")
    print(f"Sibling for writes: {m['sibling']}")
```

CDN-only dataset loader (schema-safe excerpt for `train.py`):

```python
# /opt/axentx/vanguard/train.py  (excerpt)
import json
from torch.utils.data import IterableDataset
import requests

class CDNTextDataset(IterableDataset):
    def __init__(self, manifest_path, max_files=None, schema_projector=None):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = [item["cdn_url"] for item in self.manifest["files"]]
        if max_files:
            self.files = self.files[:max_files]
        self.schema_projector = schema_projector  # callable: raw -> {prompt, response, ...}

    def __iter__(self):
        for url in self.files:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            raw = resp.content
            if self.schema_projector is not None:
                yield self.schema_projector(raw, source_url=url)
            else:
                # Default: yield raw bytes and source; project downstream
                yield {"raw": raw, "source_url": url}
```

Lightning Studio reuse guard (launcher snippet):

```python
# launcher.py (or inside notebook)
from lightning import Studio, Teamspace, Machine

def get_or_start_studio(name="vanguard-train", machine=Machine.L40S):
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(name=name, machine=machine, create_ok=True)

# Before each .run() check status and restart if stopped
studio = get_or_start_studio()
if studio.status != "Running":
    studio.start(machine=Machine.L40S)
```

### Verification (concrete steps)

1. Install deps (if not present):
   ```bash
   pip install huggingface_hub requests
   ```

2. Run discovery once (Mac/CLI) after HF rate-limit window is clear:
   ```bash
   cd /opt/axentx/vanguard
   python discovery.py --repo axentx/some-data --date-folder 2026-05-03 --out file-list.json
   ```
   - Expect: `file-list.json` created with `files[]` and `sibling` fields; no 429 errors.

3. Confirm CDN URLs work without auth:
   ```bash
   head -n 1 file-list.json | jq -r '.files[0].cdn_url' | xargs curl -I
   ```
   - Expect: `HTTP/2 200` (or `302` redirect) without auth prompts
