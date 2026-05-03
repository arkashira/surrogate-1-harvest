# surrogate-1 / frontend

## Implementation Plan — Pre-flight snapshot generator for surrogate-1

**Highest-value improvement**: Add `bin/snapshot.sh` that lists dataset files once per date folder and emits a deterministic file manifest. Embed this manifest into training so Lightning workers fetch via HF CDN (no API/auth during data load), avoiding 429s and saving quota.

### Why this ships <2h and is highest-value
- One small script + one-line change to training entrypoint.
- Eliminates repeated `list_repo_files`/`load_dataset(streaming=True)` calls during training (the main cause of 429s and HF commit pressure).
- Works with existing 16-shard runner layout; no infra changes.
- Reuses known patterns: pre-list once, embed JSON, CDN-only fetches, studio reuse.

---

### Concrete changes

1) Add `bin/snapshot.sh`
   - Inputs: `REPO`, `DATE_FOLDER`, `OUT_JSON`
   - Uses `huggingface_hub` to call `list_repo_tree(path=DATE_FOLDER, recursive=False)` once.
   - Emits `{ "date": "...", "files": [...], "generated_at": "...", "repo": "..." }`
   - Idempotent; overwrites same date folder snapshot.

2) Add `bin/lib/manifest.py`
   - Small util to read snapshot JSON and produce a stable ordered file list.
   - Optional: deterministic shard assignment by hash(slug) % N for multi-worker coordination.

3) Update training launcher / entrypoint
   - Before training starts, ensure snapshot exists (or generate on Mac orchestration node).
   - Pass manifest path to training script via env var or CLI arg.
   - In data loader: iterate manifest files and fetch each via `hf_hub_download` or raw CDN URL (`https://huggingface.co/datasets/{repo}/resolve/main/{f}`).
   - No `load_dataset(streaming=True)` on heterogeneous repo; no `list_repo_files` during training.

4) Reuse running Lightning Studio if present
   - Check `Teamspace.studios` for existing running studio with expected name; reuse instead of create.
   - If stopped, restart with `target.start(machine=Machine.L40S)` (respect free-tier fallback).

5) Cron/workflow hygiene
   - Ensure any wrapper scripts have `#!/usr/bin/env bash`, are `chmod +x`, and crontab sets `SHELL=/bin/bash`.

---

### Code snippets

#### bin/snapshot.sh
```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage: snapshot.sh <repo> <date_folder> <out_json>
# Example: snapshot.sh axentx/surrogate-1-training-pairs 2026-05-03 snapshots/2026-05-03.json

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE_FOLDER="${2:-}"
OUT_JSON="${3:-}"

if [[ -z "$DATE_FOLDER" || -z "$OUT_JSON" ]]; then
  echo "Usage: $0 <repo> <date_folder> <out_json>"
  exit 1
fi

mkdir -p "$(dirname "$OUT_JSON")"

python3 - "$REPO" "$DATE_FOLDER" "$OUT_JSON" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

def main(repo: str, date_folder: str, out_json: str) -> None:
    api = HfApi()
    # list top-level items in the date folder (non-recursive to stay light)
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]

    snapshot = {
        "repo": repo,
        "date": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
        "count": len(files),
    }

    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    print(f"Snapshot written to {out_json} ({len(files)} files)")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
PY
```

#### bin/lib/manifest.py
```python
import json
import hashlib
from pathlib import Path
from typing import List, Dict, Any

def load_manifest(manifest_path: str) -> Dict[str, Any]:
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)

def ordered_files(manifest_path: str) -> List[str]:
    data = load_manifest(manifest_path)
    return data.get("files", [])

def shard_files(manifest_path: str, shard_id: int, total_shards: int) -> List[str]:
    files = ordered_files(manifest_path)
    # deterministic by filename
    files = sorted(files, key=lambda f: hashlib.md5(f.encode()).hexdigest())
    return [f for i, f in enumerate(files) if i % total_shards == shard_id]
```

#### Training entrypoint snippet (train.py)
```python
import os
import json
from pathlib import Path
from bin.lib.manifest import ordered_files
from huggingface_hub import hf_hub_download

MANIFEST = os.getenv("SURROGATE_MANIFEST", "")
REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-training-pairs")

def cdn_fetch_file(rel_path: str, cache_dir: str = ".cache") -> str:
    # Prefer CDN raw URL to avoid auth/API during bulk reads.
    # hf_hub_download is fine (uses CDN) and handles caching.
    return hf_hub_download(repo_id=REPO, filename=rel_path, cache_dir=cache_dir)

def build_dataset_from_manifest():
    if not MANIFEST:
        raise RuntimeError("SURROGATE_MANIFEST env var required (path to snapshot JSON)")

    files = ordered_files(MANIFEST)
    examples = []
    for f in files:
        local_path = cdn_fetch_file(f)
        # Parse local_path into {prompt, response} here (project only needed cols)
        # examples.append(...)
        # Keep memory low: yield or use streaming JSONL parse as appropriate
    return examples

# Lightning Studio reuse helper (pseudo)
def ensure_or_start_studio(name: str, machine):
    from lightning import Teamspace
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            return s
    # create or start existing stopped studio
    # target = Studio(name=name, create_ok=True).start(machine=machine)
    # return target
```

#### GitHub Actions usage (optional snippet for workflow)
```yaml
- name: Generate snapshot
  run: |
    python -m pip install huggingface_hub
    bash bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-05-03 snapshots/2026-05-03.json

- name: Run training (Lightning)
  env:
    SURROGATE_MANIFEST: snapshots/2026-05-03.json
    HF_DATASET_REPO: axentx/surrogate-1-training-pairs
  run: |
    python train.py
```

---

### Rollout checklist (quick)
- [ ] `chmod +x bin/snapshot.sh`
- [ ] Add `#!/usr/bin/env bash` and `SHELL=/bin/bash` to any cron/wrapper scripts.
- [ ] Generate snapshot on orchestration node (Mac/CI) after date folder is ready.
- [ ] Update training script to accept manifest and use CDN/hf_hub_download-only loading.
- [ ] Prefer reusing running Lightning Studio; restart with L40S if stopped (respect free-tier fallback).
- [ ] Verify no `load_dataset(streaming=True)` or recursive `list_repo_files` during training.
