# vanguard / quality

### Final Consolidated Solution

**Core diagnosis**  
- Every training load triggers authenticated `list_repo_tree` calls, burning HF API quota (1000/5 min) and causing 429s.  
- No persisted `(repo, dateFolder) → file-list` manifest; each session re-enumerates via API.  
- Training scripts use HF API paths during data load instead of public CDN URLs, and lack schema-resilient reading for mixed-parquet layouts.  
- Missing idle-stop / run reuse wastes Lightning Studio quota.

**Single high-leverage change**  
Add an offline manifest generator and a CDN-only, schema-tolerant data loader; patch training to use the manifest and CDN exclusively. This is additive, <2 h, and eliminates authenticated enumeration and CDN-bypass misses.

---

### 1. Manifest generator (run once per `(repo, dateFolder)` after rate-limit window)

```bash
# /opt/axentx/vanguard/scripts/gen_manifest.sh
#!/usr/bin/env bash
set -euo pipefail
# Usage: gen_manifest.sh <repo> <date_folder> [out_dir]
# Example: gen_manifest.sh opengovus/mirror-merged 2026-04-29

REPO="${1:-opengovus/mirror-merged}"
DATEFOLDER="${2:-$(date +%Y-%m-%d)}"
OUTDIR="${3:-manifests/$(echo "$REPO" | tr / _)}"

mkdir -p "$OUTDIR"
OUTFILE="$OUTDIR/${DATEFOLDER}.json"

python3 - "$REPO" "$DATEFOLDER" "$OUTFILE" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

repo_id = sys.argv[1]
date_folder = sys.argv[2]
outfile = sys.argv[3]

api = HfApi()
items = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=True)

files = []
for item in items:
    if getattr(item, "type", None) == "file":
        fname = item.path
        files.append({
            "path": fname,
            "cdn_url": f"https://huggingface.co/datasets/{repo_id}/resolve/main/{fname}",
            "size": getattr(item, "size", None)
        })

os.makedirs(os.path.dirname(outfile), exist_ok=True)
with open(outfile, "w") as f:
    json.dump({"repo": repo_id, "date_folder": date_folder, "files": files}, f, indent=2)
print(f"Wrote {len(files)} files to {outfile}")
PY
```

```bash
chmod +x /opt/axentx/vanguard/scripts/gen_manifest.sh
```

---

### 2. CDN-only, schema-resilient loader + training integration

```python
# /opt/axentx/vanguard/train.py  (create or patch)
import json, os, io
import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import IterableDataset, DataLoader

MANIFEST_PATH = os.getenv(
    "VANGUARD_MANIFEST",
    "manifests/opengovus_mirror-merged/2026-04-29.json"
)

class CDNParquetIterable(IterableDataset):
    """
    Loads Parquet files directly from CDN URLs listed in a manifest.
    Projects to {prompt, response} at parse time to tolerate mixed schemas.
    """
    def __init__(self, manifest_path, max_files=None, shuffle_urls=False):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.urls = [f["cdn_url"] for f in manifest["files"]]
        if max_files:
            self.urls = self.urls[:max_files]
        if shuffle_urls:
            import random
            rng = random.Random(42)
            rng.shuffle(self.urls)

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            urls = self.urls
        else:
            per_worker = len(self.urls) // worker_info.num_workers
            worker_id = worker_info.id
            urls = self.urls[worker_id * per_worker : (worker_id + 1) * per_worker]

        for url in urls:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            table = pq.read_table(io.BytesIO(resp.content))

            cols = set(table.column_names)
            prompt_col = next((c for c in ("prompt", "input", "question") if c in cols), None)
            response_col = next((c for c in ("response", "output", "answer") if c in cols), None)

            if prompt_col and response_col:
                # Stream rows to avoid materializing large tables
                for batch in table.to_batches(max_chunksize=1024):
                    df = batch.to_pandas()
                    for _, row in df.iterrows():
                        yield {"prompt": row[prompt_col], "response": row[response_col]}

# Example dataloader
def make_dataloader(manifest_path, max_files=None, batch_size=8, num_workers=2):
    dataset = CDNParquetIterable(manifest_path, max_files=max_files)
    return DataLoader(dataset, batch_size=batch_size, num_workers=num_workers)

# Minimal training loop skeleton
if __name__ == "__main__":
    dl = make_dataloader(MANIFEST_PATH, max_files=None, batch_size=4, num_workers=0)
    for batch in dl:
        # Replace with your model/tokenizer/optimizer step
        prompts, responses = batch["prompt"], batch["response"]
        # train_step(prompts, responses)
        break  # demo
```

---

### 3. Operational safeguards (recommended)

- **Manifest reuse**: Set `VANGUARD_MANIFEST` to the generated JSON; training uses zero HF API calls for file listing.  
- **CDN-only downloads**: URLs use `resolve/main/...` (no Authorization header), bypassing `/api/` rate limits.  
- **Schema tolerance**: Loader projects to `{prompt, response}` dynamically; avoids CastError on mixed schemas.  
- **Lightning Studio hygiene**: Reuse existing runs when possible; add idle-stop hooks to avoid quota waste if the studio stops.  

---

### 4. Verification checklist

1. Run `gen_manifest.sh <repo> <date_folder>` and confirm `manifests/...json` exists with correct file entries.  
2. Set `VANGUARD_MANIFEST` and run `train.py`; confirm no authenticated `list_repo_tree` calls appear in logs.  
3. Monitor HF API usage; 429s should disappear during training.  
4. Confirm training iterates over samples with `prompt`/`response` fields without schema errors.
