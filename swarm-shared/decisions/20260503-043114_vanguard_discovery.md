# vanguard / discovery

## Final Synthesis (Corrected + Actionable)

### 1. Diagnosis (resolved contradictions)
- **Root cause**: no content-addressed manifest per date folder → every training run re-enumerates the repo via HF API (`list_repo_tree`/`list_repo_files`/`load_dataset`) and triggers 429s; epochs are non-reproducible; no safe resume.
- **Secondary cause**: no deterministic CDN-only fetch path; scripts mix API calls with data loading; no snapshot (file list + SHA256) to validate integrity or enable reproducible epochs.
- **Scope boundary**: fix data loading and launch orchestration; do **not** attempt to solve HF 128-commit/hr repo limits here (that requires sibling-repo sharding and commit batching, which is out of scope).

### 2. Proposed change (single coherent plan)
Add a discovery-time manifest generator and a Lightning launcher that:
- Pre-lists **one date folder once** (non-recursive), downloads each file, records `path`, `sha256`, `size`, and `cdn_url`; saves `manifest-{date}.json`.
- Embeds the manifest in training so data loading uses **CDN-only fetches** (zero HF API calls during training).
- Validates local files against manifest SHA256 before use; re-downloads on mismatch.
- Reuses a running Lightning Studio or starts one deterministically; restarts idle studios before each run.
- Keeps changes minimal and scoped to:
  - `/opt/axentx/vanguard/scripts/make_manifest.py` (new)
  - `/opt/axentx/vanguard/train.py` (modify data loader)
  - `/opt/axentx/vanguard/launch_lightning.py` (new thin wrapper)

### 3. Implementation (corrected, production-ready)

```bash
# Ensure scripts directory exists
mkdir -p /opt/axentx/vanguard/scripts
```

#### scripts/make_manifest.py
```python
#!/usr/bin/env python3
"""
Generate content-addressed manifest for a date folder in a HF dataset repo.
Usage:
  python make_manifest.py --repo datasets/my-mirror --date 2026-04-29 --out manifest-2026-04-29.json
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree, hf_hub_download
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(128 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(repo: str, date: str, out_path: str, cache_dir: str = ".cache_hf") -> None:
    """
    List one date folder (non-recursive), download each file, record sha256 + CDN URL.
    """
    os.makedirs(cache_dir, exist_ok=True)
    prefix = f"{date}/"

    try:
        entries = list_repo_tree(repo=repo, path=prefix, recursive=False)
    except Exception as e:
        print(f"Failed to list repo tree for {repo}/{prefix}: {e}")
        sys.exit(1)

    if not entries:
        print(f"No entries found under {prefix} in {repo}")
        sys.exit(1)

    manifest = {
        "repo": repo,
        "date": date,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": []
    }

    for e in entries:
        if e.type != "file":
            continue
        rel_path = e.path  # e.g. 2026-04-29/slug.parquet
        try:
            local_path = hf_hub_download(
                repo_id=repo,
                filename=rel_path,
                cache_dir=cache_dir,
                force_download=False,
                resume_download=True,
            )
        except Exception as exc:
            print(f"Failed to download {rel_path}: {exc}")
            sys.exit(1)

        digest = sha256_file(local_path)
        manifest["files"].append({
            "path": rel_path,
            "sha256": digest,
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=rel_path),
            "size": os.path.getsize(local_path),
        })
        print(f"  {rel_path} -> sha256:{digest[:12]}... size:{manifest['files'][-1]['size']}")

    out_path = Path(out_path)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate CDN-only manifest for a date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/my-mirror)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default=None, help="Output JSON path (default: manifest-{date}.json)")
    args = parser.parse_args()

    if args.out is None:
        args.out = f"manifest-{args.date}.json"

    build_manifest(args.repo, args.date, args.out)
```

#### train.py (minimal, robust patch)
Replace dataset loading section to use manifest + CDN-only fetches (no `load_dataset`/`list_repo_files` during training).

```python
# Near top of train.py
import json
import os
import hashlib
import requests
from pathlib import Path
from tqdm import tqdm
import pyarrow.parquet as pq

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(128 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def load_shards_from_manifest(manifest_path: str, cache_dir: str = ".shard_cache", timeout: int = 60):
    """
    Load shards listed in manifest via CDN URLs (zero HF API calls).
    Validates SHA256; re-downloads on mismatch or missing file.
    Yields (prompt, response) rows.
    """
    manifest = json.loads(Path(manifest_path).read_text())
    os.makedirs(cache_dir, exist_ok=True)

    for finfo in manifest["files"]:
        url = finfo["cdn_url"]
        fname = Path(finfo["path"]).name
        local = Path(cache_dir) / fname
        expected = finfo["sha256"]

        if not local.exists() or _sha256_file(str(local)) != expected:
            print(f"Fetching {fname} from CDN...")
            r = requests.get(url, stream=True, timeout=timeout)
            r.raise_for_status()
            tmp = local.with_suffix(".tmp")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=8192):
                    fh.write(chunk)
            tmp.replace(local)

            actual = _sha256_file(str(local))
            if actual != expected:
                local.unlink(missing_ok=True)
                raise RuntimeError(f"SHA256 mismatch for {fname}: expected {expected}, got {actual}")

        # Project to {prompt, response} only
        tbl = pq.read_table(str(local), columns=["prompt", "response"])
        df = tbl.to_pandas()
        for _, row in df.iterrows():
            yield row["prompt"], row["response"]
```

Then in your training loop, replace:
```python
# OLD (triggers HF API):
# from datasets import load_dataset
# ds = load_dataset("repo", split="train", streaming=True)
```
with:
```python
# NEW (CDN-only):
shards = load_shards_from_manifest("manifest-2026-04-29.json")
```

#### launch_lightning.py (wrapper)
```python
#!/usr/bin/env python3
"""
Lightning Studio launcher that reuses running studios and restarts idle
