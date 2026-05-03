# vanguard / quality

## Final Synthesis — One Correct, Actionable Plan

**Core problem**: training uses runtime HF dataset/list APIs, causing 429s, non-reproducible shard order, and unreliable resumption.  
**Goal**: eliminate runtime list/API calls; use a content-addressed, deterministic manifest + CDN-only fetches; fail fast if manifest diverges from repo.

---

## 1. Manifest builder (run on Mac after rate-limit window)

**File**: `/opt/axentx/vanguard/scripts/build_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate a deterministic, content-addressed manifest for a date folder.

Usage:
  python build_manifest.py --repo org/repo --date 2026-04-29 --out manifests/2026-04-29/filelist.json
"""
import argparse
import hashlib
import json
from pathlib import Path
from typing import Dict, List

from huggingface_hub import list_repo_tree

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(repo: str, date: str, out_path: Path, validate_files: bool = False) -> Dict:
    prefix = f"batches/mirror-merged/{date}/"
    entries = list_repo_tree(repo=repo, path=prefix, recursive=False)

    files = sorted(
        [e.rfilename for e in entries if e.type == "file" and e.rfilename.endswith(".parquet")],
        key=lambda x: (x, hashlib.sha256(x.encode()).hexdigest())
    )

    file_hashes = {}
    if validate_files:
        # Optional: download each file temporarily to compute content hash (slow but strict)
        from huggingface_hub import hf_hub_download
        for f in files:
            local = hf_hub_download(repo_id=repo, filename=f, repo_type="dataset")
            file_hashes[f] = file_sha256(Path(local))

    manifest = {
        "repo": repo,
        "date": date,
        "prefix": prefix,
        "count": len(files),
        "files": files,
        "file_hashes": file_hashes,  # empty if validate_files=False
        "sha256_manifest": "",  # filled below
        "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main/",
    }

    # Deterministic manifest hash (excluding this field)
    manifest_bytes = json.dumps(
        {k: v for k, v in manifest.items() if k != "sha256_manifest"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest["sha256_manifest"] = hashlib.sha256(manifest_bytes).hexdigest()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--validate-files", action="store_true", help="Compute per-file content hashes (slow)")
    args = p.parse_args()
    build_manifest(args.repo, args.date, args.out, validate_files=args.validate_files)
    print(f"Manifest written: {args.out}")
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

---

## 2. Manifest verifier (fail-fast before training)

**File**: `/opt/axentx/vanguard/scripts/verify_manifest.py`

```python
#!/usr/bin/env python3
"""
Verify a manifest against the live repo state.

Usage:
  python verify_manifest.py --manifest manifests/2026-04-29/filelist.json --repo org/repo
"""
import argparse
import hashlib
import json
from pathlib import Path

from huggingface_hub import list_repo_tree

def verify_manifest(manifest_path: Path, repo: str) -> bool:
    manifest = json.loads(manifest_path.read_text())
    prefix = manifest["prefix"]
    expected_files = set(manifest["files"])

    entries = list_repo_tree(repo=repo, path=prefix, recursive=False)
    live_files = {
        e.rfilename for e in entries
        if e.type == "file" and e.rfilename.endswith(".parquet")
    }

    missing = expected_files - live_files
    extra = live_files - expected_files

    if missing or extra:
        print("MANIFEST MISMATCH")
        if missing:
            print("  Missing in repo:", sorted(missing))
        if extra:
            print("  Extra in repo :", sorted(extra))
        return False

    # Recompute deterministic manifest hash
    manifest_copy = dict(manifest)
    manifest_copy.pop("sha256_manifest", None)
    recomputed = hashlib.sha256(
        json.dumps(manifest_copy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    if recomputed != manifest["sha256_manifest"]:
        print("MANIFEST HASH MISMATCH (ordering/content changed)")
        return False

    print("Manifest OK")
    return True

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--repo", required=True)
    args = p.parse_args()
    ok = verify_manifest(args.manifest, args.repo)
    exit(0 if ok else 1)
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/verify_manifest.py
```

---

## 3. Training script — use manifest + CDN-only fetches

**File**: `/opt/axentx/vanguard/train.py` (updated excerpt)

```python
import json
from pathlib import Path
from typing import Dict, List

import torch
from datasets import Dataset, DatasetDict, load_dataset
from lightning import LightningModule, Trainer
from torch.utils.data import DataLoader

MANIFEST_PATH = Path(__file__).parent / "manifests" / "2026-04-29" / "filelist.json"

def load_manifest(manifest_path: Path) -> Dict:
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Manifest missing: {manifest_path}. Run scripts/build_manifest.py first."
        )
    return json.loads(manifest_path.read_text())

def build_cdn_urls(manifest: Dict) -> List[str]:
    base = manifest["cdn_base"].rstrip("/") + "/"
    return [f"{base}{f}" for f in manifest["files"]]

class SurrogateDataModule:
    def __init__(self, manifest_path: Path, batch_size: int = 8):
        self.manifest = load_manifest(manifest_path)
        self.urls = build_cdn_urls(self.manifest)
        self.batch_size = batch_size

    def train_dataloader(self):
        # Explicit CDN URLs avoid runtime list_repo_tree/load_dataset streaming calls.
        # Using data_files with full URLs forces CDN-only fetches (no /api/ auth checks).
        ds = load_dataset(
            "parquet",
            name=self.manifest["date"],
            data_files={"train": self.urls},
            split="train",
        )
        # Keep only required columns; drop others to reduce memory/IO.
        columns_to_keep = {"prompt", "response"}
        existing = set(ds.column_names)
        to_remove = [c for c in ds.column_names if c not in columns_to_keep]
        if to_remove:
            ds = ds.remove_columns(to_remove)

        # Ensure expected fields exist (schema hygiene)
        def _clean(example):
            return {
                "prompt": str(example.get("prompt", "")),
                "response": str(example.get("response", "")),
            }
        ds = ds.map(_clean)
        return DataLoader(ds, batch_size=self.batch_size, shuffle=True, num_workers=0)

# Example training entrypoint
if __name__ == "__main__":
   
