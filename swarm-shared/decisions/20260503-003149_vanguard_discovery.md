# vanguard / discovery

## 1. Diagnosis
- No persisted `(repo, dateFolder)` manifest exists → every training run re-enumerates via authenticated HF API → quota burn and 429 risk.
- Data loader likely uses recursive `list_repo_files` or `load_dataset` during training → amplifies rate-limit exposure and slows iteration.
- No CDN-only fetch path in training code → authenticated API calls occur during data loading instead of using public CDN URLs.
- Missing orchestration wrapper to produce the manifest on the Mac (or CI) and embed it into Lightning training → forces repeated API calls inside Studio.
- No reuse guard for Lightning Studio → risk of recreating studios and burning 80hr/mo quota unnecessarily.

## 2. Proposed change
Create `/opt/axentx/vanguard/scripts/build_manifest.py` (single-purpose, <150 lines) that:
- Accepts `repo` and `dateFolder` (e.g. `2026-04-29`) as CLI args.
- Calls `list_repo_tree(path=dateFolder, recursive=False)` once.
- Persists `{repo}__{dateFolder}.json` to `/opt/axentx/vanguard/manifests/` containing CDN URLs and local basenames.
- Updates `/opt/axentx/vanguard/train.py` to accept an optional `--manifest` path and use only CDN URLs via `datasets`’s `IterableDataset` or plain `IterableWrapper` + `fsspec`/`requests` (no HF API during training).

## 3. Implementation

```bash
# create dirs
mkdir -p /opt/axentx/vanguard/{scripts,manifests}
```

`/opt/axentx/vanguard/scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Produce a CDN-only manifest for (repo, dateFolder).
Usage:
  python build_manifest.py huggingface.co/datasets/owner/repo 2026-04-29
"""
import json
import os
import sys
from pathlib import Path

import requests

HF_API_BASE = "https://huggingface.co/api"

def list_repo_tree(repo: str, path: str = "") -> list[dict]:
    """
    Non-recursive tree listing for a folder.
    repo format: datasets/owner/repo  OR  huggingface.co/datasets/owner/repo
    """
    repo = repo.replace("huggingface.co/", "")
    if not repo.startswith("datasets/"):
        repo = f"datasets/{repo}"
    url = f"{HF_API_BASE}/{repo}/tree"
    params = {"path": path, "recursive": "false"}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

def build_manifest(repo: str, date_folder: str) -> list[dict]:
    entries = list_repo_tree(repo, date_folder)
    manifest = []
    for e in entries:
        if e.get("type") != "file":
            continue
        rel_path = e["path"]
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{rel_path}"
        manifest.append(
            {
                "basename": os.path.basename(rel_path),
                "rel_path": rel_path,
                "cdn_url": cdn_url,
                "size": e.get("size"),
                "lfs": e.get("lfs", {}),
            }
        )
    return manifest

def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: build_manifest.py <repo> <dateFolder>")
        sys.exit(1)
    repo, date_folder = sys.argv[1], sys.argv[2]

    manifest = build_manifest(repo, date_folder)
    out_dir = Path(__file__).parent.parent / "manifests"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_repo = repo.replace("/", "_").replace(".", "_")
    out_path = out_dir / f"{safe_repo}__{date_folder}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest)} entries to {out_path}")

if __name__ == "__main__":
    main()
```

`/opt/axentx/vanguard/train.py` (minimal diff)
```diff
+ import json
+ from pathlib import Path
+ from typing import Optional
+
+ import fsspec
+ import requests
+ from datasets import IterableDataset, IterableDatasetDict
+ from torch.utils.data import DataLoader
+
+
+ class CDNIterableDataset(IterableDataset):
+     def __init__(self, manifest_path: Path, transform=None):
+         self.manifest = json.loads(manifest_path.read_text())
+         self.transform = transform
+
+     def __iter__(self):
+         for item in self.manifest:
+             url = item["cdn_url"]
+             # streaming-friendly: download per file; for parquet use pyarrow
+             resp = requests.get(url, timeout=30, stream=True)
+             resp.raise_for_status()
+             content = resp.content
+             # TODO: parse content (e.g., parquet/jsonl) -> {prompt, response}
+             # Example placeholder:
+             # table = pq.read_table(io.BytesIO(content))
+             # for batch in table.to_batches():
+             #    ...
+             sample = {"raw_bytes": len(content), "url": url}
+             if self.transform:
+                 sample = self.transform(sample)
+             yield sample
+
+
+ def make_dataloader(manifest_path: Optional[Path] = None, **kwargs):
+     if manifest_path is None:
+         # fallback: try to find latest manifest (for convenience)
+         manifests = sorted(Path("manifests").glob("*.json"))
+         if not manifests:
+             raise FileNotFoundError("No manifest provided and none found in manifests/")
+         manifest_path = manifests[-1]
+     ds = CDNIterableDataset(manifest_path)
+     return DataLoader(ds, **kwargs)
+
+
+ # Usage in training loop:
+ #   loader = make_dataloader(Path("manifests/datasets_owner_repo__2026-04-29.json"), batch_size=8)
```

Lightning launcher snippet (reuse guard)
```python
from lightning import Fabric, LightningFlow, LightningFlow, LightningApp
from lightning.app import LightningStudio

# Reuse existing running studio if present
def get_or_create_studio(name: str, machine: str = "L40S"):
    from lightning.app import Teamspace
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return LightningStudio(
        name=name,
        script="train.py",
        machine=machine,
        create_ok=True,
    )
```

## 4. Verification
1. Run manifest build once (Mac/CI):
   ```bash
   cd /opt/axentx/vanguard
   python scripts/build_manifest.py datasets/owner/repo 2026-04-29
   ```
   Confirm `manifests/datasets_owner_repo__2026-04-29.json` exists and contains CDN URLs.

2. Dry-run training loader locally (no HF API during iteration):
   ```bash
   python -c "
from pathlib import Path
from train import make_dataloader
loader = make_dataloader(Path('manifests/datasets_owner_repo__2026-04-29.json'), batch_size=1)
for i, b in enumerate(loader):
    print(i, b)
    if i >= 2: break
"
   ```
   Expect successful downloads from CDN and no HuggingFace API calls (check with `HF_HUB_DISABLE_TELEMETRY=1` and network logs).

3. In Lightning Studio:
   - Start or reuse a studio with the updated `train.py`.
   - Confirm training step uses only CDN URLs (monitor logs for `resolve/main/` fetches and absence of 429/authentication errors).
   - Confirm studio reuse behavior: running `get_or_create_studio` twice returns the same running instance without creating a new one.
