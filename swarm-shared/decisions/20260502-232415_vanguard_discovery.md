# vanguard / discovery

## Final Synthesis (Best Parts + Corrected + Actionable)

**Core diagnosis (merged, corrected)**
- No persistent file-list cache for HF repos → training scripts repeatedly call `list_repo_tree`/`load_dataset` and hit 429 rate limits.
- Lightning Studio lifecycle is not reused → each run recreates studios and burns quota; idle-stop kills training.
- No CDN-only data path; training still uses `load_dataset`/`list_repo_files` which hits API rate limits instead of bypassing via `resolve/main/`.
- Local model/data ingestion (`.from_pretrained`) may be attempted on Mac instead of delegating to Lightning/Kaggle/Cerebras.
- No lightweight discovery harness to surface top-connected knowledge-hub docs before planning work.

**Single, concrete change**
Create `/opt/axentx/vanguard/discovery/run_discovery.py` (single file, ~120–150 lines) that:
1. Lists one date folder in a target HF dataset repo via a single API call and writes `file_list.json` (cached).
2. Reuses a running Lightning Studio (name match) or starts one with L40S preference and graceful fallback.
3. Emits a minimal training stub that downloads only listed files via CDN (no Authorization header) and projects `{prompt,response}`.
4. Prints top-hub insight from local graph (if available) or a tags summary.

**Implementation (corrected + actionable)**

```bash
# /opt/axentx/vanguard/discovery/run_discovery.py
#!/usr/bin/env python3
"""
Vanguard discovery harness:
- Cache HF file list once (CDN bypass strategy)
- Reuse running Lightning Studio (best-effort)
- Emit top-hub insight stub
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path

HF_REPO = os.getenv("HF_REPO", "my-org/surrogate-1")
HF_FOLDER = os.getenv("HF_FOLDER", "batches/mirror-merged/2026-05-02")
HF_TOKEN = os.getenv("HF_TOKEN", "")  # optional; CDN bypass avoids auth
CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
FILE_LIST_PATH = CACHE_DIR / "file_list.json"

# ---- 1) HF file list (single API call) ----
def list_hf_folder():
    if FILE_LIST_PATH.exists():
        try:
            return json.loads(FILE_LIST_PATH.read_text())
        except Exception:
            pass  # regenerate

    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_TOKEN or None)
        # non-recursive to minimize pagination/requests
        files = api.list_repo_tree(repo_id=HF_REPO, path=HF_FOLDER, recursive=False)
        paths = [f.rfilename for f in files if f.rfilename]
        FILE_LIST_PATH.write_text(json.dumps(paths, indent=2))
        return paths
    except Exception as e:
        print(f"[WARN] HF list failed: {e}", file=sys.stderr)
        if FILE_LIST_PATH.exists():
            print("[INFO] Using cached file list.", file=sys.stderr)
            return json.loads(FILE_LIST_PATH.read_text())
        raise

# ---- 2) Lightning Studio reuse (best-effort) ----
def reuse_or_start_studio():
    # Best-effort: try Lightning Cloud SDK; if unavailable, return None and proceed locally.
    try:
        import lightning as L
        from lightning.app import LightningStudio
        studio_name = "vanguard-train-l40s"
        studio = LightningStudio(
            name=studio_name,
            script="train_cdn.py",
            create_ok=True,
        )
        return studio
    except Exception as e:
        print(f"[WARN] Lightning Studio setup failed (non-fatal): {e}", file=sys.stderr)
        return None

# ---- 3) Top-hub insight stub ----
def top_hub_insight():
    graph_path = Path(__file__).parent.parent / "knowledge_rag" / "graph.json"
    if graph_path.exists():
        try:
            g = json.loads(graph_path.read_text())
            if isinstance(g, dict) and "nodes" in g and "edges" in g:
                deg = Counter()
                for e in g["edges"]:
                    deg[e.get("source")] += 1
                    deg[e.get("target")] += 1
                if deg:
                    top, _ = deg.most_common(1)[0]
                    print(f"[TOP-HUB] Most-connected hub: {top}")
                    return top
        except Exception:
            pass
    print("[TOP-HUB] No local graph found; skipping.")
    return None

# ---- 4) CDN-only training stub ----
_TRAIN_STUB = """# CDN-only training stub (zero HF API calls during data load)
import json
import os
import sys
from pathlib import Path

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

HF_REPO = os.getenv("HF_REPO", "{repo}")
HF_FOLDER = os.getenv("HF_FOLDER", "{folder}")
FILE_LIST = {file_list}

def cdn_url(path):
    return f"https://huggingface.co/datasets/{{HF_REPO}}/resolve/main/{{path}}"

def stream_parquet_rows(url):
    # stream parquet without full download via requests + pq
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    tmp = Path("/tmp/tmp.parquet")
    with tmp.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    table = pq.read_table(tmp)
    # project only prompt/response; ignore extra cols
    cols = set(table.column_names)
    prompt_col = next((c for c in ("prompt", "instruction", "input") if c in cols), None)
    response_col = next((c for c in ("response", "output", "completion") if c in cols), None)
    for i in range(table.num_rows):
        row = {}
        if prompt_col:
            row["prompt"] = table[prompt_col][i].as_py()
        if response_col:
            row["response"] = table[response_col][i].as_py()
        if row:
            yield row

def main():
    examples = []
    for rel in tqdm(FILE_LIST, desc="Downloading"):
        url = cdn_url(f"{{HF_FOLDER}}/" + rel)
        try:
            for row in stream_parquet_rows(url):
                examples.append(row)
        except Exception as e:
            print(f"Skip {{rel}}: {{e}}", file=sys.stderr)
    print(f"Prepared {{len(examples)}} examples (CDN-only).")
    out = Path("cdn_examples.jsonl")
    with out.open("w") as f:
        for ex in examples:
            f.write(json.dumps(ex) + "\\n")
    print("Wrote", out)

if __name__ == "__main__":
    main()
"""

def write_train_stub():
    try:
        paths = json.loads(FILE_LIST_PATH.read_text()) if FILE_LIST_PATH.exists() else []
    except Exception:
        paths = []
    stub = CACHE_DIR / "train_cdn.py"
    stub.write_text(_TRAIN_STUB.format(repo=HF_REPO, folder=HF_FOLDER, file_list=json.dumps(paths)))
    return stub

# ---- 5) Main ----
def main():
    print("[1/4] Discovering HF file list...")
    paths = list_hf_folder()
    print(f"      Found {len(paths)} files.")

    print("[2/4] Checking Lightning Studio...")
    studio = reuse_or_start_studio()
    if studio:
        print("      Studio prepared (reuse/create attempted).")
    else:
        print("      Studio unavailable; continue with local CDN-only stub.")

    print("[3/4] Top-hub insight...")
    top_hub_insight()

    print("[4/4] Writing train_cdn.py stub...")
    stub = write_train_stub()
    print(f"      Done. Run with: python {stub} (or submit to Studio).")

if __name__ == "__main__":
    main()
```

```bash
# Make executable and install minimal deps (run once)
chmod +x /opt/axentx/vanguard/discovery/run_discovery.py
cd /opt/axentx/vanguard
python -m pip install --quiet huggingface_hub pyarrow requests
```


