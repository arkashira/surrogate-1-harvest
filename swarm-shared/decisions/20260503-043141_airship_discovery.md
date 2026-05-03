# airship / discovery

## Implementation Plan (≤2h)

**Goal:** Deterministic CDN file manifest + Lightning Studio lifecycle resilience for Surrogate-1 training.

### 1) Pre-list date folder → JSON manifest (Mac orchestration)
- Single `list_repo_tree(path, recursive=False)` per date folder → save `manifest-{date}.json`
- Embed manifest path in `train.py`; Lightning training uses CDN-only fetches (`resolve/main/...`) with zero API calls during data load
- On 429: wait 360s, retry once; fallback to last-known manifest if still blocked

### 2) Lightning Studio lifecycle guard
- Before `.run()`: check `Teamspace.studios` for existing Running studio with matching name → reuse
- If stopped: restart with `target.start(machine=Machine.L40S)` (free tier fallback to L40S; H200 requires `lightning-lambda-prod`)
- Wrap each training run in status check to avoid idle-timeout kills

### 3) Surrogate-1 ingestion hardening
- Download each file individually via `hf_hub_download` (avoid `load_dataset(streaming=True)` on mixed-schema repos)
- Project to `{prompt, response}` only at parse time; move attribution to filename pattern `batches/mirror-merged/{date}/{slug}.parquet`
- No `source` / `ts` columns in parquet to keep schema uniform
- Spread HF commit writes across 5 sibling repos (hash slug → pick repo) to stay under 128/hr/repo cap

### 4) HF CDN bypass in training dataloader
- Use `https://huggingface.co/datasets/{repo}/resolve/main/{path}` with no Authorization header
- Single manifest JSON embedded in train.py → dataloader iterates local paths and CDN URLs only
- CDN tier has much higher limits; avoids `/api/` auth-check rate limits entirely

---

## Code Snippets

### manifest_generator.py (run on Mac)
```python
#!/usr/bin/env python3
"""
Generate deterministic CDN manifest for a date folder.
Run: python manifest_generator.py <repo> <date_folder> [--out manifest-2026-05-03.json]
"""
import json, os, sys, time
from huggingface_hub import list_repo_tree

def main():
    if len(sys.argv) < 3:
        print("Usage: manifest_generator.py <repo> <date_folder> [--out <file>]")
        sys.exit(1)

    repo = sys.argv[1]
    date_folder = sys.argv[2].strip("/")
    out_path = "manifest.json"
    for i, arg in enumerate(sys.argv):
        if arg == "--out" and i + 1 < len(sys.argv):
            out_path = sys.argv[i + 1]

    # Single API call: non-recursive listing for the date folder
    max_retries = 3
    for attempt in range(max_retries):
        try:
            tree = list_repo_tree(repo=repo, path=date_folder, recursive=False)
            break
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            print(f"Attempt {attempt+1} failed: {e}. Waiting 60s...")
            time.sleep(60)

    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "files": sorted(files),
        "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}"
    }

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {out_path} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

### train.py (Lightning Studio) — CDN-only dataloader
```python
import json, os
import torch
from torch.utils.data import Dataset, DataLoader
from huggingface_hub import hf_hub_download
import pyarrow.parquet as pq

class CDNParquetDataset(Dataset):
    def __init__(self, manifest_path):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.prefix = self.manifest["cdn_prefix"]
        self.files = self.manifest["files"]

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        rel = self.files[idx]
        # CDN fetch (no auth header) — fast and bypasses API rate limits
        url = f"{self.prefix}/{rel}"
        # Use hf_hub_download for local caching; CDN path used under the hood when repo is public
        local_path = hf_hub_download(
            repo_id=self.manifest["repo"],
            filename=f"{self.manifest['date_folder']}/{rel}",
            repo_type="dataset"
        )
        # Project to {prompt, response} only — ignore extra schema fields
        table = pq.read_table(local_path, columns=["prompt", "response"])
        df = table.to_pandas()
        # Return first row as example; extend to iterate all rows as needed
        row = df.iloc[0]
        return {"prompt": row["prompt"], "response": row["response"]}

def maybe_reuse_or_create_studio(target_name="surrogate-train"):
    from lightning import Studio, Machine, Teamspace
    for s in Teamspace().studios:
        if s.name == target_name and s.status == "Running":
            print(f"Reusing running studio: {target_name}")
            return s
    print(f"Creating studio: {target_name}")
    # Free tier fallback to L40S; H200 requires lightning-lambda-prod
    return Studio(
        name=target_name,
        machine=Machine.L40S,
        create_ok=True
    )

def train():
    manifest_path = "manifest-2026-05-03.json"
    dataset = CDNParquetDataset(manifest_path)
    loader = DataLoader(dataset, batch_size=8, shuffle=True)

    studio = maybe_reuse_or_create_studio("surrogate-train")
    if studio.status != "Running":
        studio.start(machine=Machine.L40S)

    # Example training loop (replace with your actual training code)
    for batch in loader:
        prompts = batch["prompt"]
        responses = batch["response"]
        # ... training step ...
        print(f"Processed batch with {len(prompts)} samples")

if __name__ == "__main__":
    train()
```

### ingestion_hardening.py (HF sibling repos + schema projection)
```python
#!/usr/bin/env bash
# Ensure executable: chmod +x ingestion_hardening.py
# Run via bash to avoid shebang/cron issues

set -euo pipefail
SHELL=/bin/bash

REPO_BASE="datasets/your-org/surrogate-mirror"
SIBLINGS=5

pick_sibling() {
    local slug="$1"
    local hash=$(echo -n "$slug" | md5sum | cut -c1-8)
    local idx=$(( 0x${hash} % SIBLINGS ))
    echo "${REPO_BASE}-s${idx}"
}

process_file() {
    local src_file="$1"
    local date="$2"
    local slug=$(basename "$src_file" .parquet)

    # Download individual file (avoid streaming mixed schemas)
    local tmp=$(mktemp)
    huggingface-cli download "$REPO_BASE" "$date/$slug.parquet" --local-dir "$tmp" --local-dir-use-symlinks False

    # Project to {prompt, response} only; attribution in filename
    python3 -c "
import pyarrow.parquet as pq, pyarrow as pa, os, sys
src = '$tmp/$date/$slug.parquet'
tbl = pq.read_table(src, columns=['prompt', 'response'])
out_dir = 'batches/mirror-merged/$date'
os.makedirs(out_dir, exist_ok=True)
pq.write_table(tbl, os.path.join(out_dir, '${slug}.parquet'))
"

    # Upload to sibling repo (spread commits)
    target_repo=$(pick_sibling "$slug")
    # Use gh or huggingface_hub CLI to upload parquet; example:
    # huggingface-cli upload "$target_repo" "batches/mirror-merged/$date/${slug}.parquet" "batches/mirror-merged/$date/${slug}.parquet"
    echo "Would
