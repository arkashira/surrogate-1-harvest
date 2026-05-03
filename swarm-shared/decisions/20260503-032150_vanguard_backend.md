# vanguard / backend

## Final Synthesized Solution

**Core diagnosis (accepted from both):**  
No CDN-first, content-addressed manifest exists; training scripts can still trigger `list_repo_tree`/`load_dataset` at runtime, causing 429s, quota burn, and non-reproducible runs. Lightning Studio workers lack a pinned artifact to avoid runtime discovery.

**Single source of truth:**  
Create `/opt/axentx/vanguard/backend/manifest.py` (CLI) and patch/create `/opt/axentx/vanguard/backend/train.py` so:

- Mac orchestration generates one manifest per date (CDN URLs + content hash + size).  
- Training consumes **only** CDN URLs (zero HF API calls) and verifies content integrity.  
- Manifest is passed explicitly to Lightning Studio via env var; workers never call HF APIs.

---

### 1) Manifest generator (single file, robust)

```bash
# /opt/axentx/vanguard/backend/manifest.py
#!/usr/bin/env python3
"""
Generate CDN-first, content-addressed manifest for a HF dataset repo folder.

Outputs:
  manifests/manifest-{date}.json
{
  "repo": "...",
  "date": "YYYY-MM-DD",
  "generated_at": "...Z",
  "files": [
    {
      "path": "...",
      "size": 123,
      "sha256": "...",
      "cdn_url": "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    }
  ]
}
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree, hf_hub_download
except ImportError:
    sys.exit("Install: pip install huggingface_hub")

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def _retry_list(repo_id: str, path: str, retries: int = 2, wait: int = 300):
    for attempt in range(1, retries + 1):
        try:
            return list_repo_tree(repo_id=repo_id, path=path, recursive=True)
        except Exception as exc:
            if attempt == retries:
                raise
            print(f"HF API error (attempt {attempt}/{retries}): {exc}", file=sys.stderr)
            time.sleep(wait)

def _file_sha256(repo_id: str, path: str) -> str:
    # Deterministic content hash without relying on etag; download once per manifest build.
    local_path = hf_hub_download(repo_id=repo_id, filename=path)
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(repo: str, date_folder: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = []

    tree = _retry_list(repo, date_folder)
    for item in tree:
        if item.type != "file":
            continue
        path = item.path
        sha256 = _file_sha256(repo, path)
        entries.append({
            "path": path,
            "size": getattr(item, "size", None),
            "sha256": sha256,
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=path)
        })

    manifest = {
        "repo": repo,
        "date": os.path.basename(date_folder.rstrip("/")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": entries
    }

    out_path = out_dir / f"manifest-{manifest['date']}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(entries)} files -> {out_path}")
    return out_path

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN-first manifest")
    parser.add_argument("--repo", required=True, help="HF dataset repo id")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out-dir", default="manifests", help="Output directory")
    args = parser.parse_args()

    date_folder = f"batches/mirror-merged/{args.date}"
    build_manifest(args.repo, date_folder, Path(args.out_dir))

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/backend/manifest.py
```

---

### 2) Training script (CDN-only, reproducible)

```bash
# /opt/axentx/vanguard/backend/train.py
import json
import os
import hashlib
import requests
from pathlib import Path

def load_manifest(date_slug: str):
    manifest_path = Path(__file__).parent / "manifests" / f"manifest-{date_slug}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest missing: {manifest_path}")
    return json.loads(manifest_path.read_text())

def _verify(sha256: str, data: bytes) -> bool:
    return hashlib.sha256(data).hexdigest() == sha256

def cdn_data_generator(manifest):
    """
    Lightning Studio worker does CDN-only fetches; zero HF API calls during training.
    Yields {"prompt": ..., "response": ...} after project-specific parsing.
    """
    for f in manifest["files"]:
        resp = requests.get(f["cdn_url"], timeout=30)
        resp.raise_for_status()
        payload = resp.content
        if not _verify(f["sha256"], payload):
            raise ValueError(f"Integrity check failed: {f['path']}")

        # Project-specific: parse surrogate-1 format to {prompt, response}
        # Replace with actual parsing logic.
        # Example stub:
        #   obj = json.loads(payload)
        #   yield {"prompt": obj["prompt"], "response": obj["response"]}
        yield {"raw_bytes": payload, "path": f["path"]}

def main():
    date_slug = os.environ.get("HF_DATASET_DATE")
    if not date_slug:
        raise RuntimeError("Set HF_DATASET_DATE to load manifest")
    manifest = load_manifest(date_slug)
    for sample in cdn_data_generator(manifest):
        # Replace with real training step
        print(sample["path"])

if __name__ == "__main__":
    main()
```

---

### 3) Mac orchestration + Lightning Studio launcher

```python
# launcher.py  (run from Mac orchestration)
import os
import subprocess
from pathlib import Path

# 1) Generate manifest (once per date)
repo = "your-dataset-org/vanguard-data"
date_slug = "2026-05-03"
backend_dir = Path("/opt/axentx/vanguard/backend")
manifest_path = backend_dir / "manifests" / f"manifest-{date_slug}.json"

if not manifest_path.exists():
    subprocess.run([
        sys.executable, str(backend_dir / "manifest.py"),
        "--repo", repo,
        "--date", date_slug,
        "--out-dir", str(backend_dir / "manifests")
    ], check=True, cwd=backend_dir)

# 2) Start Lightning Studio with pinned manifest
from lightning import Studio

studio = Studio(
    name="vanguard-train",
    script="backend/train.py",
    machine="L40S",
    env={
        "MANIFEST_PATH": str(manifest_path),
        "HF_DATASET_DATE": date_slug,
        "HF_HUB_OFFLINE": "1"   # enforce CDN-only in workers
    },
    create_ok=True
)
studio.run()
```

---

### 4) Verification checklist

1. **Generate manifest**  
   ```bash
   cd /opt/axentx/vanguard/backend
   python3 manifest.py --repo your-dataset-org/vanguard-data --date 2026-05-03 --out-dir manifests
   ```
   Confirm `manifests/manifest-2026-05-03.json` exists with `cdn_url` and
