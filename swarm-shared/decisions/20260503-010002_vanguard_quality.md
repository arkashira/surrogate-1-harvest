# vanguard / quality

## 1. Diagnosis
- No persisted `(repo, dateFolder)` manifest: every training run re-enumerates via authenticated HF API → quota burn + 429 risk.
- Training script likely still uses `load_dataset(streaming=True)` on heterogeneous repos → pyarrow `CastError` on mixed schemas.
- Data ingestion writes extra metadata columns (`source`, `ts`) and keeps raw nested files in `enriched/` → schema pollution and downstream training instability.
- No CDN-only fetch path: training still makes per-epoch API calls instead of using `resolve/main/` CDN URLs (bypasses auth and rate limits).
- Lightning Studio reuse not enforced: scripts likely create new studios instead of reusing running ones → wastes ~80hr/mo quota.

## 2. Proposed change
Create `/opt/axentx/vanguard/training/manifest.py` (single, focused file) that:
- Builds and caches a `(repo, dateFolder)` file manifest via one HF API call (`list_repo_tree`, non-recursive).
- Persists manifest as JSON alongside training artifacts.
- Exposes CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) for zero-auth, zero-API data loading during training.
- Filters to only `{prompt, response}` projection at parse time (avoids mixed-schema issues).

## 3. Implementation

```bash
# /opt/axentx/vanguard/training/manifest.py
import json
import os
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    from huggingface_hub import list_repo_tree, hf_hub_download
except ImportError:
    list_repo_tree = None
    hf_hub_download = None


class HFManifestBuilder:
    """
    Build and cache a CDN-only file manifest for a repo+dateFolder.
    - Single non-recursive list_repo_tree call (paginated safely).
    - Persists manifest JSON to avoid repeated API calls.
    - Provides CDN URLs to bypass auth/rate limits during training.
    """

    CDN_BASE = "https://huggingface.co/datasets"

    def __init__(self, repo: str, date_folder: str, cache_root: Optional[str] = None):
        self.repo = repo
        self.date_folder = date_folder.lstrip("/")
        self.cache_root = Path(cache_root or Path.cwd() / ".manifest_cache")
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self._manifest_path()

    def _manifest_path(self) -> Path:
        slug = f"{self.repo}__{self.date_folder}".replace("/", "__")
        return self.cache_root / f"{slug}.json"

    def _cache_key(self, tree: List[Dict]) -> str:
        # crude etag: hash of repo+date+file-count+last-sizes
        payload = f"{self.repo}|{self.date_folder}|{len(tree)}|{sum(t.get('size',0) for t in tree)}"
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def build(self, force: bool = False) -> Dict:
        """
        Returns manifest:
        {
          "repo": "...",
          "date_folder": "...",
          "generated_at": "...",
          "etag": "...",
          "files": [
            {"path": "...", "size": 123, "cdn_url": "...", "local_path": "..."},
            ...
          ]
        }
        """
        if not force and self.manifest_path.exists():
            with open(self.manifest_path) as f:
                cached = json.load(f)
            # lightweight freshness: if same day and non-empty, reuse
            if cached.get("files") and cached.get("date_folder") == self.date_folder:
                return cached

        if list_repo_tree is None:
            raise RuntimeError("huggingface_hub not installed")

        # Single non-recursive call per dateFolder (avoids heavy recursion/pagination)
        tree = list_repo_tree(repo_id=self.repo, path=self.date_folder, recursive=False)
        files = []
        for entry in tree:
            if entry.get("type") != "file":
                continue
            path = entry["path"]
            # Only include likely data files (parquet/jsonl) to reduce noise
            if not (path.endswith(".parquet") or path.endswith(".jsonl")):
                continue
            cdn_url = f"{self.CDN_BASE}/{self.repo}/resolve/main/{path}"
            files.append({
                "path": path,
                "size": entry.get("size", 0),
                "cdn_url": cdn_url,
                "local_path": None  # populated if downloaded
            })

        manifest = {
            "repo": self.repo,
            "date_folder": self.date_folder,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "etag": self._cache_key(files),
            "files": files
        }

        with open(self.manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        return manifest

    def download_file(self, file_entry: Dict, target_dir: Path) -> Path:
        """
        Download a single file via CDN-like path using hf_hub_download (or raw HTTP).
        Projects to {prompt, response} at parse time — caller should parse only needed fields.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        local_path = hf_hub_download(
            repo_id=self.repo,
            filename=file_entry["path"],
            cache_dir=str(target_dir),
            force_download=False
        )
        file_entry["local_path"] = local_path
        return Path(local_path)


def build_or_load_manifest(repo: str, date_folder: str, cache_root: Optional[str] = None, force: bool = False) -> Dict:
    builder = HFManifestBuilder(repo=repo, date_folder=date_folder, cache_root=cache_root)
    return builder.build(force=force)


if __name__ == "__main__":
    # Example usage (run from Mac orchestration only)
    import sys
    repo = sys.argv[1] if len(sys.argv) > 1 else "databricks/databricks-dolly-15k"
    date_folder = sys.argv[2] if len(sys.argv) > 2 else "2024-01-01"
    manifest = build_or_load_manifest(repo, date_folder, force=False)
    print(json.dumps(manifest, indent=2))
```

Update training launcher (example snippet to embed manifest and use CDN-only paths):

```python
# /opt/axentx/vanguard/training/train.py  (excerpt)
import json
from pathlib import Path
from manifest import build_or_load_manifest

def prepare_data(repo: str, date_folder: str):
    manifest = build_or_load_manifest(repo, date_folder, cache_root=".manifest_cache", force=False)
    # Pass CDN URLs to Lightning dataset loader; training uses raw HTTP (no HF API calls).
    return [f["cdn_url"] for f in manifest["files"]]

# In Lightning DataModule:
#   Use fsspec + requests (or datasets with streaming=False + split downloads) to fetch via CDN URLs.
#   Parse only {prompt, response} fields at load time to avoid mixed-schema errors.
```

Lightning Studio reuse guard (orchestration snippet):

```python
# launcher.py (run on Mac)
from lightning import Teamspace, Studio, Machine

def reuse_or_create_studio(name: str):
    for s in Teamspace.studios():
        if s.name == name and s.status == "Running":
            return s
    return Studio(
        name=name,
        machine=Machine.L40S,
        create_ok=True
    )
```

## 4. Verification
1. Run manifest builder once:
   ```bash
   cd /opt/axentx/vanguard/training
   python manifest.py databricks/databricks-dolly-15k 2024-01-01 > out.json
   ```
   Confirm `out.json` contains `files[]` with `cdn_url` fields and no auth required when fetching one URL via `curl -I`.

2. Confirm no API calls during training:
   - Start a Lightning Studio (L40S) and run training with the manifest JSON present.
   - Monitor HF API rate-limit headers or logs; there should be zero authenticated `list_repo_*` or dataset API calls after manifest generation.

3. Schema safety check:
   - Ensure
