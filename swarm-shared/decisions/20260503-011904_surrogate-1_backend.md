# surrogate-1 / backend

## Implementation Plan (≤2h)

Highest-value incremental improvement:  
Replace recursive HF API ingestion and per-file authenticated fetches with a single non-recursive `list_repo_tree` + CDN-only fetches during training. This removes rate-limit pressure, avoids 429s, and keeps Lightning training API-call-free.

### Steps (ordered)

1. Add `list_repo_tree` helper to orchestrator (Mac) and save manifest JSON.
2. Update `bin/dataset-enrich.sh` to accept a manifest file and use CDN URLs (`resolve/main/...`) for downloads.
3. Update training script (`train.py`) to read the manifest and stream via CDN (no `hf_hub_download`/API calls).
4. Add fallback/retry and logging for CDN failures.
5. Update GitHub Actions workflow to pass manifest path and shard mapping.
6. Smoke-test locally (or via workflow_dispatch).

Estimated effort: ~90–110 minutes.

---

## Code Snippets

### 1) Orchestrator: produce manifest (run on Mac)

```python
# tools/build_manifest.py
import json
import os
import sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
OUT_DIR = "manifests"
os.makedirs(OUT_DIR, exist_ok=True)

api = HfApi()

def build_manifest_for_date(date_folder: str):
    """
    date_folder example: "2026-04-29"
    Uses non-recursive list_repo_tree to avoid pagination/rate limits.
    """
    entries = api.list_repo_tree(
        repo_id=HF_REPO,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )

    files = []
    for e in entries:
        if not e.path.endswith((".parquet", ".jsonl", ".csv")):
            continue
        cdn_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{e.path}"
        files.append({
            "path": e.path,
            "cdn_url": cdn_url,
            "size": getattr(e, "size", None),
        })

    manifest = {
        "date_folder": date_folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo": HF_REPO,
        "files": files,
        "total_files": len(files),
    }

    out_path = os.path.join(OUT_DIR, f"manifest-{date_folder}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {out_path} ({len(files)} files)")
    return out_path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python build_manifest.py <date_folder>")
        sys.exit(1)
    build_manifest_for_date(sys.argv[1])
```

---

### 2) Worker: use CDN URLs from manifest

```bash
# bin/dataset-enrich.sh
# Accept MANIFEST_JSON env or arg.
# Downloads via CDN (no auth/API) and projects to {prompt,response}.

set -euo pipefail

MANIFEST="${MANIFEST_JSON:-}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
OUT_DIR="output"
mkdir -p "$OUT_DIR"

if [ -z "$MANIFEST" ] || [ ! -f "$MANIFEST" ]; then
  echo "MANIFEST_JSON must point to a valid manifest file"
  exit 1
fi

# Deterministic shard assignment by file path hash
python3 - "$MANIFEST" "$SHARD_ID" "$TOTAL_SHARDS" "$OUT_DIR" <<'PY'
import json, hashlib, os, sys, subprocess, tempfile, itertools, pyarrow.parquet as pq

manifest_path, shard_id, total_shards, out_dir = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
shard_id = int(shard_id)

with open(manifest_path) as f:
    manifest = json.load(f)

files = manifest["files"]
os.makedirs(out_dir, exist_ok=True)

def assign_shard(path: str) -> int:
    h = int(hashlib.md5(path.encode()).hexdigest(), 16)
    return h % total_shards

selected = [f for f in files if assign_shard(f["path"]) == shard_id]
print(f"Shard {shard_id}/{total_shards}: processing {len(selected)} files")

def normalize_to_pairs(file_info):
    url = file_info["cdn_url"]
    path = file_info["path"]
    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(path)[1], delete=False) as tmp:
        # CDN download (no auth)
        subprocess.run(["curl", "-fsSL", "-o", tmp.name, url], check=True)
        tmp_path = tmp.name
    try:
        # Project to prompt/response only; ignore extra columns
        if path.endswith(".parquet"):
            table = pq.read_table(tmp_path, columns=["prompt", "response"])
            df = table.to_pandas()
        elif path.endswith(".jsonl"):
            import pandas as pd
            df = pd.read_json(tmp_path, lines=True)
            if "prompt" not in df.columns or "response" not in df.columns:
                # Best-effort column selection
                df = df.rename(columns={c: c.lower() for c in df.columns})
                if "prompt" not in df.columns or "response" not in df.columns:
                    return []
        else:
            # CSV fallback
            import pandas as pd
            df = pd.read_csv(tmp_path)
            df.columns = [c.lower() for c in df.columns]

        pairs = []
        for _, row in df.iterrows():
            prompt = str(row.get("prompt", ""))
            response = str(row.get("response", ""))
            if prompt.strip() and response.strip():
                pairs.append({"prompt": prompt, "response": response})
        return pairs
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

all_pairs = []
for f in selected:
    try:
        pairs = normalize_to_pairs(f)
        all_pairs.extend(pairs)
    except Exception as exc:
        print(f"Failed {f['path']}: {exc}")

ts = manifest["created_at"].replace(":", "").split("T")[0]
out_file = os.path.join(out_dir, f"shard{shard_id}-{ts}.jsonl")
with open(out_file, "w", encoding="utf-8") as f:
    for p in all_pairs:
        f.write(json.dumps(p, ensure_ascii=False) + "\n")

print(f"Shard {shard_id} wrote {len(all_pairs)} pairs to {out_file}")
PY
```

---

### 3) Training script: consume manifest and stream via CDN

```python
# train.py
import json
import pyarrow.parquet as pq
import pandas as pd
import tempfile
import subprocess
from torch.utils.data import IterableDataset, DataLoader
import torch

class CDNPairDataset(IterableDataset):
    def __init__(self, manifest_path):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = self.manifest["files"]

    def _stream_file(self, file_info):
        url = file_info["cdn_url"]
        path = file_info["path"]
        with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False) as tmp:
            subprocess.run(["curl", "-fsSL", "-o", tmp.name, url], check=True)
            tmp_path = tmp.name
        try:
            if path.endswith(".parquet"):
                table = pq.read_table(tmp_path, columns=["prompt", "response"])
                df = table.to_pandas()
            elif path.endswith(".jsonl"):
                df = pd.read_json(tmp_path, lines=True)
            else:
                df = pd.read_csv(tmp_path)
            for _, row in df.iterrows():
                prompt = str(row.get("prompt", ""))
                response = str(row.get("response", ""))
                if prompt.strip() and response.strip():
                    yield {"prompt": prompt, "response": response}
        finally:
            try:
                subprocess.run(["rm", "-f", tmp_path],
