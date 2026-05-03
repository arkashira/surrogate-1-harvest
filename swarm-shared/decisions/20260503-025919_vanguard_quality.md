# vanguard / quality

## Final synthesized implementation (single, coherent plan)

**Core principle:** Deterministic, CDN-first delivery with a strict orchestration/compute boundary and automated guardrails.

---

### 1) Diagnosis (resolved)
- **Missing deterministic manifest** → solved by a build-time asset pipeline that produces a content-hashed manifest once per run.
- **No orchestration/compute boundary** → solved by explicit sentinel documentation + runtime checks that prevent training code from executing on the orchestration host.
- **No integrity checks** → solved by storing `sha256` in the manifest and validating on download.
- **Risk of runtime HF API calls during training** → solved by forbidding `load_dataset` and requiring CDN URLs from the manifest; manifest is generated once on the orchestration host.

---

### 2) Architecture (minimal, high-leverage)

```
/opt/axentx/vanguard/
├─ scripts/
│  └─ build_assets.py          # run on orchestration host (Mac)
├─ assets/
│  ├─ manifest.json            # generated; CDN URLs + sha256 + size
│  └─ README.md                # contract
├─ training/
│  ├─ train.py                 # imports manifest; CDN-only loader
│  └─ _guard.py                # prevents training on orchestration host
├─ frontend/
│  └─ asset_loader.js          # consumes manifest; no HF API calls
├─ ORCHESTRATOR.md             # host vs compute contract
└─ .gitignore                  # ignore .cache/ and local temp files
```

---

### 3) Implementation

#### 3.1 Orchestration/compute boundary guard
```python
# /opt/axentx/vanguard/training/_guard.py
import os
import sys

def ensure_not_orchestrator():
    """
    Prevent training on orchestration host.
    Override by setting ALLOW_TRAIN_ON_HOST=1 for exceptional cases.
    """
    if os.getenv("ALLOW_TRAIN_ON_HOST") == "1":
        return

    markers = [
        "/System/Library",               # macOS system paths
        "/opt/homebrew",                 # macOS Homebrew
        os.path.expanduser("~/Library"), # macOS user Library
    ]
    if any(os.path.exists(m) for m in markers):
        print("ERROR: Refusing to run training on orchestration host (macOS).")
        print("Run this on Lightning Studio / compute target.")
        print("To override (not recommended): ALLOW_TRAIN_ON_HOST=1")
        sys.exit(1)
```

#### 3.2 Build-time asset manifest (orchestration host)
```python
#!/usr/bin/env python3
# /opt/axentx/vanguard/scripts/build_assets.py
"""
Generate deterministic CDN-first asset manifest.
Run on orchestration host (Mac). Commit manifest to repo.
"""
import json, hashlib, os, sys, time
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree, hf_hub_download
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

REPO_ID = os.getenv("HF_ASSETS_REPO", "axentx/vanguard-assets")
DATE_FOLDER = os.getenv("ASSETS_DATE_FOLDER", "2026-05-03")
OUT_DIR = Path(os.getenv("OUT_DIR", "../assets")).resolve()
OUT_FILE = OUT_DIR / "manifest.json"

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(128 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def build_manifest():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Listing {REPO_ID}/{DATE_FOLDER} (non-recursive)...")
    try:
        items = list_repo_tree(REPO_ID, path=DATE_FOLDER, recursive=False)
    except Exception as e:
        print(f"HF API error: {e}")
        print("If 429, wait 360s and retry. CDN downloads do not count against API limit.")
        raise

    entries = []
    for item in items:
        if item.get("type") != "file":
            continue
        rel_path = f"{DATE_FOLDER}/{item['path']}"
        local_path = Path(
            hf_hub_download(repo_id=REPO_ID, filename=rel_path, cache_dir=str(OUT_DIR / ".cache"))
        )
        digest = sha256_file(local_path)
        size = local_path.stat().st_size
        cdn_url = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{rel_path}"
        entries.append({
            "sha256": digest,
            "size": size,
            "path": rel_path,
            "cdn_url": cdn_url,
            "local_cache": str(local_path)
        })
        print(f"  {rel_path} -> {digest[:12]}... ({size} bytes)")

    manifest = {
        "repo_id": REPO_ID,
        "date_folder": DATE_FOLDER,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(entries),
        "entries": entries
    }

    OUT_FILE.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {OUT_FILE}")
    return manifest

if __name__ == "__main__":
    build_manifest()
```

#### 3.3 Training loader (compute target only)
```python
# /opt/axentx/vanguard/training/train.py
"""
Training boundary — CDN-only data loading.
Run on compute target (Lightning Studio). Do NOT run on orchestration host.
"""
import json
from pathlib import Path

from ._guard import ensure_not_orchestrator
ensure_not_orchestrator()

MANIFEST_PATH = Path(__file__).parent.parent / "assets" / "manifest.json"
if not MANIFEST_PATH.exists():
    raise RuntimeError(
        "Missing assets manifest. On orchestration host (Mac), run: "
        "cd scripts && python build_assets.py"
    )

with open(MANIFEST_PATH) as f:
    ASSETS = json.load(f)

def validate_sha256(local_path: Path, expected: str) -> bool:
    import hashlib
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(128 * 1024), b""):
            h.update(chunk)
    return h.hexdigest() == expected

def make_cdn_dataloader(batch_size=8, max_workers=4):
    """
    Build a DataLoader that streams from CDN URLs listed in manifest.
    Uses local cache when available; validates sha256; falls back to CDN.
    No Hugging Face API calls during training.
    """
    import torch
    from torch.utils.data import DataLoader, IterableDataset
    import requests
    from concurrent.futures import ThreadPoolExecutor

    class CDNIterable(IterableDataset):
        def __init__(self, entries):
            self.entries = entries

        def _stream_one(self, entry):
            local_cache = Path(entry.get("local_cache"))
            if local_cache and local_cache.is_file():
                if validate_sha256(local_cache, entry["sha256"]):
                    return local_cache.read_bytes()
                else:
                    print(f"Integrity mismatch: {local_cache}")

            resp = requests.get(entry["cdn_url"], timeout=30)
            resp.raise_for_status()
            data = resp.content
            # optional: validate sha256 on downloaded bytes
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError(f"Integrity check failed: {entry['path']}")
            return data

        def __iter__(self):
            # simple threadpool for concurrent downloads
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                for data in ex.map(self._stream_one, self.entries):
                    # project to {prompt, response} or tensors here
                    yield {"text": data.decode("utf-8", errors="replace")}

    urls = [e for e in ASSETS["entries"]]
    dataset = CDNIterable(urls)
    return DataLoader(dataset, batch_size=batch_size, num
