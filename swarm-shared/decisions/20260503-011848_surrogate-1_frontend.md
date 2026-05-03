# surrogate-1 / frontend

## Final synthesized implementation (best of both proposals)

**Core idea (highest value, <2h):**  
Pre-compute a deterministic, non-recursive file list per date folder on the Mac orchestrator, embed it in the HF Space (or pass as an artifact), and have the Space/training script fetch **only via CDN URLs** with strict `{prompt,response}` projection. This eliminates recursive HF API calls, per-file auth/429s, and keeps dedup unchanged.

---

## Concrete plan (≤2h)

1. **Mac orchestrator** — one-time per date folder  
   - Run `bin/gen-filelist.sh <date>` (or `list_folder.py`)  
   - Produces `assets/filelist-<date>.json` (committed to repo or passed as artifact)  
   - Uses `list_repo_tree(..., recursive=False)` → list of filenames only  
   - No recursive crawling, no per-file API calls

2. **HF Space UI** (`app.py`)  
   - Detect `assets/filelist-*.json` and expose selector  
   - Show date, file count, estimated steps  
   - “Build splits & start training” button → passes manifest to training  
   - Cache manifest in `st.session_state`; display “CDN-only mode: ON”

3. **Training script** (`train.py` or equivalent)  
   - Load manifest (date + files)  
   - Build CDN URLs:  
     `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/batches/public-merged/{date}/{name}`  
   - Use `datasets.load_dataset("json", data_files=urls, streaming=True)` (no auth)  
   - Project each record to `{prompt, response}` (drop all other fields)  
   - Optional: local SQLite dedup check via existing `lib/dedup.py` if desired (central store remains source-of-truth)

4. **Dedup & schema**  
   - Keep `lib/dedup.py` unchanged (central SQLite on HF Space)  
   - No schema changes to stored shards; projection happens at parse time only

5. **Workflow integration**  
   - Matrix jobs (or local runners) receive date folder + file-list artifact (or embed)  
   - Each runner uses CDN-only ingestion; no `list_repo_files` during training

---

## Resolved contradictions (correctness + actionability)

- **Recursive vs non-recursive listing**: Use non-recursive `list_repo_tree(..., recursive=False)` once per date folder (both proposals agree; this is correct and avoids API churn).  
- **Where to store file list**: Embed in repo under `assets/` (committed by Mac) for simplicity and reproducibility; optionally allow artifact pass-through for CI matrix jobs. Both approaches supported; repo embed is the default because it’s zero-config for Space reruns.  
- **Auth during training**: Strict CDN-only URLs (no HF API, no tokens) during data loading. This eliminates 429s and is compatible with public datasets layout.  
- **Projection timing**: Project to `{prompt,response}` at parse time in the training script, not during file listing. Keeps file list simple and schema-agnostic.  
- **Dedup**: Central `lib/dedup.py` remains source-of-truth; Space/training may optionally run local SQLite check for cross-run duplicates but must not diverge from central logic.

---

## Code snippets (final)

### 1) Mac orchestrator — generate filelist (one-liner script)

`bin/gen-filelist.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail
REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="assets/filelist-${DATE}.json"
mkdir -p assets

python3 - <<PY
import json, os, sys
from huggingface_hub import HfApi
api = HfApi()
repo = os.environ.get("REPO", "axentx/surrogate-1-training-pairs")
date = sys.argv[1]
items = api.list_repo_tree(repo=repo, path=f"batches/public-merged/{date}", recursive=False)
files = [it.path for it in items if it.type == "file"]
with open(sys.argv[2], "w") as f:
    json.dump({"date": date, "files": sorted(files)}, f, indent=2)
print(f"Wrote {len(files)} files -> {sys.argv[2]}")
PY "$DATE" "$OUT"
```

### 2) Space UI — select manifest and launch

`app.py`
```python
import streamlit as st
import json, pathlib, subprocess, os

st.title("Surrogate-1 Trainer (CDN-only)")
assets_dir = pathlib.Path("assets")
filelists = sorted(assets_dir.glob("filelist-*.json"))

if not filelists:
    st.warning("No filelist JSON in assets/. Run bin/gen-filelist.sh <date> first.")
    st.stop()

choice = st.selectbox("Date filelist", [f.name for f in filelists])
with open(f"assets/{choice}") as f:
    manifest = json.load(f)

st.caption(f"Date: {manifest['date']} | Files: {len(manifest['files'])}")
if st.button("Build splits & start training"):
    st.session_state.manifest = manifest
    # Run training inline or via subprocess
    res = subprocess.run(
        [sys.executable, "train.py", f"assets/{choice}"],
        capture_output=True, text=True
    )
    st.text(res.stdout)
    if res.stderr:
        st.error(res.stderr)
```

### 3) Training script — CDN-only loader + projection

`train.py`
```python
import json, os, sys
from datasets import load_dataset

def build_cdn_urls(repo, date, files):
    base = f"https://huggingface.co/datasets/{repo}/resolve/main"
    return [f"{base}/batches/public-merged/{date}/{os.path.basename(f)}" for f in files]

def project(batch):
    # Normalize schema variants to {prompt, response}
    return {
        "prompt": batch.get("prompt") or batch.get("input") or "",
        "response": batch.get("response") or batch.get("output") or "",
    }

def train(manifest_path, repo="axentx/surrogate-1-training-pairs"):
    with open(manifest_path) as f:
        manifest = json.load(f)

    date = manifest["date"]
    files = manifest["files"]
    urls = build_cdn_urls(repo, date, files)

    ds = load_dataset(
        "json",
        data_files={"train": urls},
        streaming=True,
        split="train",
    )
    ds = ds.map(project, batched=False)

    # Example: iterate and optionally dedup against central store
    total = 0
    for _ in ds:
        total += 1
        if total % 1000 == 0:
            print(f"Processed {total} rows")
    print(f"Total rows available: {total}")

if __name__ == "__main__":
    train(sys.argv[1] if len(sys.argv) > 1 else "assets/filelist-latest.json")
```

---

## Acceptance checklist

- [ ] `bin/gen-filelist.sh <date>` produces valid `assets/filelist-<date>.json`  
- [ ] `list_repo_tree(..., recursive=False)` is used (no recursive API calls)  
- [ ] `app.py` lists manifests and passes to training  
- [ ] `train.py` uses CDN-only URLs (no authenticated HF API calls during streaming)  
- [ ] Projection keeps only `{prompt, response}`; schema variants normalized at parse time  
- [ ] `lib/dedup.py` remains unchanged and is source-of-truth for dedup  
- [ ] UI shows “CDN-only mode: ON”, file count, and estimated steps before training  

Ship this to eliminate HF API rate limits during data loading while staying fully compatible with the existing shard layout and dedup pipeline.
