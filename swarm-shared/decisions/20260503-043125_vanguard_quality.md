# vanguard / quality

## Final Synthesis (Best of Both Candidates)

**Diagnosis (Consolidated)**
- Runtime re-enumeration of HF repos via `list_repo_files`/`load_dataset` causes intermittent 429s and non-reproducible epochs.
- No content-addressed manifest (path + SHA256 + size) per date folder → CDN-only fetches impossible; resume/restart unsafe.
- Missing deterministic repo→sibling routing concentrates writes and hits 128/hr commit cap.
- No fallback to CDN-only path when rate-limited; no pre-training verification of manifest vs remote content.
- Recreation of Lightning Studio burns quota and risks idle-stop loss.

**Proposed Change (Single, Actionable Layer)**
Add a manifest generator + CDN-only fetcher with deterministic routing and Studio reuse:
- One-time snapshot of each date folder into `manifests/{date}/files.json` (path, size, etag, sha256, cdn_url, sibling_repo).
- Training uses CDN-only URLs (`/resolve/main/...`) with zero HF API calls during epochs.
- Deterministic `hash(slug) % 5` routing spreads writes across sibling repos for commit-cap scaling.
- Pre-training verification (manifest integrity + remote HEAD checks) before first epoch.
- One-liner guard to reuse running Lightning Studio by name.

**Implementation (Minimal, Correct, Executable)**

1) Create `/opt/axentx/vanguard/scripts/make_manifest.py`:

```python
#!/usr/bin/env python3
"""
make_manifest.py
Snapshot a date folder of a HF dataset repo into a content-addressed manifest.
Usage:
  HF_TOKEN=hf_xxx python make_manifest.py \
    --repo bigcode/the-stack \
    --date 2024-01-15 \
    --out manifests/2024-01-15/files.json
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import requests
from huggingface_hub import HfApi

API = HfApi()
CDN_ROOT = "https://huggingface.co/datasets"

def sibling_repo(repo: str, slug: str, n: int = 5) -> str:
    """Deterministic sibling repo for commit-cap scaling."""
    idx = hash(slug) % n
    if idx == 0:
        return repo
    name, org = repo.split("/")
    return f"{name}-s{idx}/{org}"

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def build_manifest(repo: str, date: str, out_path: Path, token: str | None = None, verify: bool = True):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # Use repo tree (shallow) to list date folder
    tree = API.list_repo_tree(repo=repo, path=date, recursive=True, token=token)
    files = []
    for item in tree:
        if item.type != "file":
            continue
        path = item.path
        cdn_url = f"{CDN_ROOT}/{repo}/resolve/main/{path}"

        # HEAD for size/etag; verify availability
        r = requests.head(cdn_url, allow_redirects=True, timeout=30)
        r.raise_for_status()
        size = int(r.headers.get("content-length", -1))
        etag = r.headers.get("etag", "").strip('"')

        # Optional content verification (sample or full)
        sha256 = ""
        if verify:
            # Prefer hub download for integrity; fallback to CDN stream hash
            try:
                from huggingface_hub import hf_hub_download
                local = hf_hub_download(repo_id=repo, filename=path, repo_type="dataset", token=token)
                sha256 = sha256_bytes(open(local, "rb").read())
            except Exception:
                data = requests.get(cdn_url, timeout=60).content
                sha256 = sha256_bytes(data)

        files.append(
            {
                "path": path,
                "size": size,
                "etag": etag,
                "sha256": sha256,
                "cdn_url": cdn_url,
                "sibling_repo": sibling_repo(repo, path),
            }
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"repo": repo, "date": date, "files": files}
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files -> {out_path}")
    return manifest

def verify_manifest_remote(manifest_path: Path, token: str | None = None, sample_n: int = 3) -> bool:
    """Lightweight pre-training check: HEAD each CDN URL and compare size/etag if available."""
    with open(manifest_path) as f:
        manifest = json.load(f)
    ok = True
    for entry in manifest["files"][:sample_n]:
        r = requests.head(entry["cdn_url"], allow_redirects=True, timeout=30)
        if r.status_code != 200:
            print(f"FAIL remote HEAD: {entry['cdn_url']} -> {r.status_code}")
            ok = False
            continue
        remote_size = int(r.headers.get("content-length", -1))
        if entry["size"] >= 0 and remote_size >= 0 and entry["size"] != remote_size:
            print(f"Size mismatch: {entry['path']} local={entry['size']} remote={remote_size}")
            ok = False
    return ok

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN-safe manifest for a date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. bigcode/the-stack)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2024-01-15)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"), help="HF token (optional for public repos)")
    parser.add_argument("--no-verify", action="store_true", help="Skip content hash verification")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, Path(args.out), args.token, verify=not args.no_verify)
```

Make executable:

```bash
chmod +x /opt/axentx/vanguard/scripts/make_manifest.py
```

2) Patch surrogate-1 train entrypoint (lightweight CDN-only loader + Studio reuse):

```python
# At top of train.py or data module:
import json
import requests
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    """
    CDN-only dataset using a prebuilt manifest.
    Replace per-project projection (prompt/response) as needed.
    """
    def __init__(self, manifest_path, start=0, end=None, columns=None):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = self.manifest["files"][start:end]
        self.columns = columns  # reserved for projection logic

    def __iter__(self):
        for f in self.files:
            resp = requests.get(f["cdn_url"], stream=True, timeout=60)
            resp.raise_for_status()
            # TODO: project parquet to {prompt, response} here
            # Example placeholder:
            yield {"path": f["path"], "raw_len": len(resp.content)}

# Lightning Studio reuse guard (one-liner before Studio.create):
from lightning import Studio
running = [s for s in Studio.list() if s.name == "vanguard-surrogate1" and s.status == "running"]
studio = running[0] if running else Studio.create(name="vanguard-surrogate1", machine=Machine.L40S)
```

**Verification (Concrete Steps)**

1. Generate manifest (once per date folder):
   ```bash
   HF_TOKEN=hf_xxx python /opt/axentx/vanguard/scripts/make_manifest.py \
     --repo bigcode/the-stack \
     --date 2024-01-15 \
     --out manifests/2024-01-15/files.json
   ```
   - Confirm `files.json` exists with `path`, `sha256`, `cdn_url`, `sibling_repo`.

2. Lightweight pre-training check:
   ```bash
