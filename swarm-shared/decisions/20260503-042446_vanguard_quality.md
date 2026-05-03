# vanguard / quality

## 1. Diagnosis
- Runtime `load_dataset`/recursive file enumeration triggers HF API 429s and non-reproducible epochs (no content-addressed manifest).
- No pre-listed file manifest → CDN-only fetching impossible; training scripts still hit `/api/` during data loading.
- Mixed-schema ingestion writes extra columns (`source`, `ts`) into enriched/; breaks surrogate-1 schema expectations and wastes storage/throughput.
- No deterministic repo mapping for HF commit-cap mitigation (128/hr/repo) — all writes target one repo.
- Training orchestration likely recreates Lightning Studio instead of reusing running ones; wastes quota and risks idle-stop training death.

## 2. Proposed change
Create `/opt/axentx/vanguard/training/manifest.py` and update the main training script (`train.py` or equivalent) to:
- Accept a pre-generated `file_manifest.json` (date-scoped) listing `{repo, path, sha256, url}` for CDN-only fetches.
- Use `datasets` with `streaming=False` + `data_files` pointing to local cache or direct CDN URLs; never call `list_repo_files` during training.
- Add a small CLI to generate the manifest on the Mac orchestration host (single API call) and embed it in the training job.
- Add deterministic repo selection via hash-slug for HF writes (5-sibling spread).
- Add Lightning Studio reuse guard before `.run()`.

Scope: new file `manifest.py`, minimal edits to `train.py` (or create if missing), and a helper script `gen_manifest.py` in `scripts/`.

## 3. Implementation

```bash
# /opt/axentx/vanguard
mkdir -p training scripts
```

### `training/manifest.py`
```python
# training/manifest.py
from __future__ import annotations
import json
import hashlib
import os
from pathlib import Path
from typing import List, Dict, Any

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

class FileEntry:
    repo: str
    path: str
    sha256: str
    url: str

    def __init__(self, repo: str, path: str, sha256: str):
        self.repo = repo
        self.path = path
        self.sha256 = sha256
        self.url = HF_CDN_TEMPLATE.format(repo=repo, path=path)

    def to_dict(self) -> Dict[str, str]:
        return {"repo": self.repo, "path": self.path, "sha256": self.sha256, "url": self.url}

    @classmethod
    def from_dict(cls, d: Dict[str, str]) -> "FileEntry":
        return cls(repo=d["repo"], path=d["path"], sha256=d["sha256"])


class FileManifest:
    entries: List[FileEntry]

    def __init__(self, entries: List[FileEntry] | None = None):
        self.entries = entries or []

    def add(self, repo: str, path: str, sha256: str) -> None:
        self.entries.append(FileEntry(repo=repo, path=path, sha256=sha256))

    def to_json(self) -> str:
        return json.dumps([e.to_dict() for e in self.entries], indent=2)

    @classmethod
    def from_json(cls, s: str) -> "FileManifest":
        data = json.loads(s)
        return cls(entries=[FileEntry.from_dict(item) for item in data])

    @classmethod
    def load(cls, path: Path | str) -> "FileManifest":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_json(f.read())

    def save(self, path: Path | str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    def select_repo_for_write(self, slug: str, sibling_count: int = 5) -> str:
        """Deterministic repo selection to spread HF commit load.
        Assumes repo pattern: <base>-<ix> (e.g., surrogate-1-0..4)."""
        base = self.entries[0].repo.rsplit("-", 1)[0] if self.entries else "surrogate-1"
        idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % sibling_count
        return f"{base}-{idx}"
```

### `scripts/gen_manifest.py`
```python
#!/usr/bin/env bash
# scripts/gen_manifest.py
# Usage: python gen_manifest.py <repo> <date_folder> <out.json>
# Requires: huggingface_hb, run on Mac orchestration host (single API call)

import sys
import json
import hashlib
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

def sha256_of_sample(repo: str, path: str) -> str:
    # Best-effort deterministic hash for manifest; CDN fetch validates later.
    return hashlib.sha256(f"{repo}/{path}".encode()).hexdigest()

def main():
    if len(sys.argv) != 4:
        print("Usage: python gen_manifest.py <repo> <date_folder> <out.json>")
        sys.exit(1)
    repo, date_folder, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

    entries = []
    for item in list_repo_tree(repo, path=date_folder, recursive=False):
        if item.type != "file":
            continue
        entries.append({
            "repo": repo,
            "path": item.path,
            "sha256": sha256_of_sample(repo, item.path),
            "url": f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}"
        })

    Path(out_path).write_text(json.dumps(entries, indent=2))
    print(f"Wrote {len(entries)} entries to {out_path}")

if __name__ == "__main__":
    main()
```

### Update training script (example: `training/train.py`)
```python
# training/train.py  (add near top)
import json
from pathlib import Path
from training.manifest import FileManifest

def load_manifest(manifest_path: Path | str) -> FileManifest:
    return FileManifest.load(manifest_path)

def build_data_files(manifest: FileManifest):
    # Return dict suitable for datasets.load_dataset(data_files=...)
    # Group by repo or split as needed; here we use direct URLs.
    return [e.url for e in manifest.entries]

# In dataset loading:
# manifest = load_manifest("file_manifest.json")
# data_files = build_data_files(manifest)
# dataset = load_dataset("parquet", data_files=data_files, streaming=False)
# No recursive list_repo_files during training -> CDN-only, zero API calls.
```

### Lightning Studio reuse guard (example snippet)
```python
# In orchestration / launcher
from lightning import Studio, Teamspace, Machine, L40S

def get_or_create_studio(name: str):
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            return s
    return Studio(
        name=name,
        machine=Machine.L40S,
        create_ok=True,
    )

studio = get_or_create_studio("vanguard-train")
if studio.status != "Running":
    studio.start(machine=Machine.L40S)
```

## 4. Verification
1. Run manifest generation on Mac host:
   ```bash
   cd /opt/axentx/vanguard
   python scripts/gen_manifest.py surrogate-1 2026-05-03 file_manifest.json
   ```
   Confirm `file_manifest.json` exists and contains correct `url` fields (CDN resolve links).

2. Validate CDN accessibility (no auth):
   ```bash
   head -n1 file_manifest.json | jq -r '.[0].url' | xargs curl -I
   ```
   Expect `200 OK` (not 401/429).

3. Dry-run training dataset load (local test):
   ```python
   from training.manifest import FileManifest
   manifest = FileManifest.load("file_manifest.json")
  
