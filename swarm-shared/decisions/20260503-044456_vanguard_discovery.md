# vanguard / discovery

## 1. Diagnosis
- No content-addressed manifest per date folder → runtime `list_repo_tree`/`load_dataset` calls during training trigger HF API 429s and produce non-reproducible epochs.
- Missing deterministic `{path, sha256}` snapshot for each date folder → training cannot resume or verify bitwise-identical data across runs.
- Data ingestion writes mixed-schema files into `enriched/` with extra metadata columns (`source`, `ts`) → downstream `load_dataset` fails on schema drift and wastes I/O.
- No CDN-only path for training → every epoch re-authenticates against `/api/` endpoints instead of using public CDN URLs, burning rate-limit budget.
- No lightweight verification step to confirm CDN file availability before training starts → late failures after quota is already spent on setup.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/manifest.py` + `/opt/axentx/vanguard/discovery/build_manifest.py` that:
- Runs once per date folder (post-ingestion) on the Mac orchestrator.
- Calls `list_repo_tree(path, recursive=False)` once, saves `manifest-{date}.json` with `{file_path, sha256, size, cdn_url}`.
- Projects each file to `{prompt, response}` only (drops extra cols) and stores as `{slug}.parquet` in `batches/mirror-merged/{date}/`.
- Embeds the manifest path into training scripts so Lightning Studio can do CDN-only fetches with zero API calls during data load.

## 3. Implementation

```bash
# /opt/axentx/vanguard/discovery/build_manifest.py
#!/usr/bin/env bash
set -euo pipefail

# Usage: build_manifest.sh <date> <hf_repo> <out_dir>
# Example: build_manifest.sh 2026-05-03 axentx/vanguard-data ./manifests

DATE="${1:-$(date +%Y-%m-%d)}"
REPO="${2:-axentx/vanguard-data}"
OUT_DIR="${3:-./manifests}"
MANIFEST="${OUT_DIR}/manifest-${DATE}.json"
BATCH_DIR="batches/mirror-merged/${DATE}"

mkdir -p "$(dirname "${MANIFEST}")"

python3 - "$REPO" "$DATE" "$MANIFEST" "$BATCH_DIR" <<'PY'
import json, os, hashlib, sys
from huggingface_hub import list_repo_tree, hf_hub_download

REPO, DATE, MANIFEST, BATCH_DIR = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
os.makedirs(BATCH_DIR, exist_ok=True)

entries = list_repo_tree(REPO, path=DATE, recursive=False)
manifest = []

for e in entries:
    if e.type != "file":
        continue
    path = e.path
    if not path.endswith((".jsonl", ".parquet", ".json")):
        continue

    # Download once, project to {prompt, response}, store as canonical parquet
    local_path = hf_hub_download(repo_id=REPO, filename=path, repo_type="dataset")
    sha256 = hashlib.sha256(open(local_path, "rb").read()).hexdigest()
    size = os.path.getsize(local_path)

    # Project to {prompt, response} and write canonical file
    import pandas as pd
    if path.endswith(".parquet"):
        df = pd.read_parquet(local_path)
    elif path.endswith(".jsonl"):
        df = pd.read_json(local_path, lines=True)
    else:
        df = pd.read_json(local_path)

    # Keep only prompt/response; drop source/ts/other metadata
    keep = [c for c in df.columns if c in {"prompt", "response"}]
    if len(keep) < 2:
        # fallback: first two cols
        keep = df.columns[:2].tolist()
    df = df[keep].rename(columns={keep[0]: "prompt", keep[-1]: "response"})

    slug = path.rpartition("/")[-1].split(".")[0]
    out_file = os.path.join(BATCH_DIR, f"{slug}.parquet")
    df.to_parquet(out_file, index=False)

    manifest.append({
        "file_path": path,
        "sha256": sha256,
        "size": size,
        "cdn_url": f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}",
        "canonical_path": out_file
    })

with open(MANIFEST, "w") as f:
    json.dump({"date": DATE, "repo": REPO, "files": manifest}, f, indent=2)

print(f"Manifest written to {MANIFEST}")
PY
```

```python
# /opt/axentx/vanguard/discovery/manifest.py
import json
from pathlib import Path
from typing import List, Dict

class Manifest:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text())

    @property
    def date(self) -> str:
        return self.data["date"]

    @property
    def repo(self) -> str:
        return self.data["repo"]

    @property
    def files(self) -> List[Dict]:
        return self.data["files"]

    def cdn_only_urls(self) -> List[str]:
        return [f["cdn_url"] for f in self.files]

    def canonical_paths(self) -> List[str]:
        return [f["canonical_path"] for f in self.files]

    def verify_cdn(self, timeout: int = 5) -> Dict[str, bool]:
        import requests
        ok = {}
        for f in self.files:
            try:
                r = requests.head(f["cdn_url"], timeout=timeout, allow_redirects=True)
                ok[f["file_path"]] = r.status_code == 200
            except Exception:
                ok[f["file_path"]] = False
        return ok
```

Update training launcher to use manifest (example snippet for Lightning):

```python
# launcher.py (excerpt)
from vanguard.discovery.manifest import Manifest

manifest = Manifest("./manifests/manifest-2026-05-03.json")
assert all(manifest.verify_cdn().values()), "Some CDN files unavailable"

# Lightning dataset module reads only CDN URLs or canonical parquet files
# No HF API calls during training.
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/discovery/build_manifest.py
```

## 4. Verification
1. Run once: `./discovery/build_manifest.py 2026-05-03 axentx/vanguard-data ./manifests`
2. Confirm `manifest-2026-05-03.json` exists and contains `files[]` with `cdn_url` and `canonical_path`.
3. Confirm `batches/mirror-merged/2026-05-03/*.parquet` exist and have exactly columns `prompt`, `response`.
4. In Python: `from vanguard.discovery.manifest import Manifest; m=Manifest("./manifests/manifest-2026-05-03.json"); assert all(m.verify_cdn().values())`
5. Launch a minimal Lightning training run that uses only `m.canonical_paths()` or `m.cdn_only_urls()` and monitor HF API usage — should remain at 0 calls during data loading epochs.
