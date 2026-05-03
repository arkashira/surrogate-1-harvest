# vanguard / discovery

## Final Synthesis & Actionable Plan

**Core diagnosis (unified):**  
- No content-addressed, per-date manifest → training and ingestion enumerate via HF API at runtime, causing 429s, non-reproducible epochs, and quota waste.  
- No deterministic `{path, sha256, size, url}` snapshot → CDN fetches can’t be validated/resumed and training can’t run with zero API calls.  
- No separation between Mac (list once, produce manifest) and Lightning (CDN-only, zero API) → violates Mac=CLI + remote compute pattern and exposes training to rate limits.  
- No idempotent ingestion that projects heterogeneous files to `{prompt, response}` before training → risks pyarrow `CastError` on mixed schemas.  
- Missing lightweight validation and schema checks.

**Chosen approach:**  
Adopt Candidate 1’s `manifest.py` and CDN-first training pattern, add Candidate 2’s validation layer and Candidate 3’s schema projection, with concrete fixes for correctness and actionability.

---

## 1) Create `/opt/axentx/vanguard/discovery/manifest.py`

Single-purpose tool: produce a content-addressed snapshot for one date folder.

```bash
mkdir -p /opt/axentx/vanguard/discovery
```

`/opt/axentx/vanguard/discovery/manifest.py`
```python
#!/usr/bin/env python3
"""
Produce content-addressed manifest for one date folder in a HuggingFace dataset repo.
Usage:
  python manifest.py <repo> <date> <out.json> [folder_prefix]
Example:
  python manifest.py datasets/mycorp/vanguard 2024-05-01 manifest-2024-05-01.json
"""
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

import requests
from tqdm import tqdm

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def sha256_file(path: str, chunk_kb: int = 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_kb * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_date_folder(repo: str, date: str, out_path: str, folder_prefix: str = "") -> List[Dict]:
    """
    List one date folder (non-recursive), download via CDN, compute sha256, write manifest.
    Returns entries: [{"path":..., "sha256":..., "size":..., "url":...}, ...]
    """
    from huggingface_hub import list_repo_tree, hf_hub_download

    entries = []
    target_path = f"{folder_prefix}{date}" if folder_prefix else date
    print(f"Listing repo tree: {repo}/{target_path} (non-recursive)")

    try:
        tree = list_repo_tree(repo=repo, path=target_path, recursive=False)
    except Exception as e:
        print(f"Error listing repo tree: {e}")
        # Fallback: try to list root if date folder not found
        tree = list_repo_tree(repo=repo, path="", recursive=False)
        tree = [t for t in tree if t.path.startswith(date)]

    files = [t for t in tree if t.type == "file"]
    if not files:
        print("No files found for date folder; trying root-level matches...")
        # Best-effort: include any file containing date in name
        all_tree = list_repo_tree(repo=repo, path="", recursive=False)
        files = [t for t in all_tree if t.type == "file" and date in t.path]

    print(f"Found {len(files)} files. Downloading via CDN and checksumming...")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    for ft in tqdm(files, desc="Files"):
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=ft.path)
        local_path = hf_hub_download(repo_id=repo, filename=ft.path, repo_type="dataset")
        digest = sha256_file(local_path)
        size = os.path.getsize(local_path)
        entries.append({
            "path": ft.path,
            "sha256": digest,
            "size": size,
            "url": cdn_url,
        })

    manifest = {
        "repo": repo,
        "date": date,
        "generated_by": "vanguard/discovery/manifest.py",
        "entries": entries,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"Manifest written to {out_path} ({len(entries)} entries)")
    return entries


def main() -> None:
    if len(sys.argv) < 4:
        print("Usage: python manifest.py <repo> <date> <out.json> [folder_prefix]")
        sys.exit(1)
    repo, date, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    folder_prefix = sys.argv[4] if len(sys.argv) > 4 else ""
    snapshot_date_folder(repo, date, out_path, folder_prefix=folder_prefix)


if __name__ == "__main__":
    main()
```

Make executable and ensure deps:
```bash
chmod +x /opt/axentx/vanguard/discovery/manifest.py
pip install huggingface_hub tqdm --quiet 2>/dev/null || true
```

---

## 2) Add lightweight validation: `/opt/axentx/vanguard/discovery/validate.py`

Verifies manifest entries against CDN and schema.

`/opt/axentx/vanguard/discovery/validate.py`
```python
#!/usr/bin/env python3
"""
Validate a manifest:
- Each entry has required keys: path, sha256, size, url
- HEAD/GET checks on CDN URLs (optional)
- Optional: re-checksum local cache if present
"""
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List

import requests
from tqdm import tqdm


def validate_manifest(manifest_path: str, check_cdn: bool = True, rehash_local: bool = False) -> bool:
    with open(manifest_path) as f:
        manifest = json.load(f)

    entries: List[Dict] = manifest.get("entries", [])
    required = {"path", "sha256", "size", "url"}
    ok = True

    for ent in tqdm(entries, desc="Validating"):
        if not required.issubset(ent.keys()):
            print(f"Missing keys in entry: {ent}")
            ok = False
            continue

        if check_cdn:
            try:
                r = requests.head(ent["url"], allow_redirects=True, timeout=10)
                if r.status_code not in (200, 302):
                    print(f"CDN HEAD failed for {ent['path']}: {r.status_code}")
                    ok = False
            except Exception as e:
                print(f"CDN check error for {ent['path']}: {e}")
                ok = False

        if rehash_local:
            # If file is in HF cache, re-checksum
            from huggingface_hub import hf_hub_download
            try:
                local_path = hf_hub_download(repo_id=manifest["repo"], filename=ent["path"], repo_type="dataset")
                h = hashlib.sha256()
                with open(local_path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                if h.hexdigest() != ent["sha256"]:
                    print(f"Checksum mismatch for {ent['path']}")
                    ok = False
            except Exception:
                pass  # file may not be cached; skip rehash

    if ok:
        print("Manifest validation passed.")
    else:
        print("Manifest validation failed.")
    return ok


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python validate.py <manifest.json> [--no-cdn] [--rehash]")
        sys.exit(1)
    manifest_path = sys.argv[1]
    check_c
