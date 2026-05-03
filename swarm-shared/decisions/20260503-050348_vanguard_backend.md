# vanguard / backend

## Final Synthesis (Best Parts + Correctness + Actionability)

I merged the strongest, non-redundant insights from both candidates and resolved contradictions in favor of **correctness** (content-addressing, schema stability, rate-limit avoidance) and **concrete actionability** (exact CLI, env vars, and code).

---

## 1. Diagnosis (Consolidated)
- **Rate limits & non-reproducibility**: Runtime `datasets`/`list_repo_tree` calls expose training to 429s and non-deterministic shard order.
- **Schema instability**: Runtime projection of mixed-schema parquet risks `pyarrow.CastError` and wastes CPU.
- **Quota waste**: Lightning Studio is recreated instead of reused (~80 hr/mo setup/teardown burn).
- **HF write limits**: No deterministic repo→sibling mapping concentrates ingestion commits and risks the 128-commit repo cap.
- **No content-addressing**: Missing per-date manifest makes resumption unreliable and epochs drift.

---

## 2. Proposed Change (Scope & Goal)
- **Scope**:
  - Add `/opt/axentx/vanguard/manifest.py` (run once per date folder on Mac orchestration).
  - Update `/opt/axentx/vanguard/train.py` to embed manifest and use CDN-only fetches + studio reuse + sibling-aware writes.
- **Goal**:
  - Generate content-addressed manifest per date folder once.
  - Switch Lightning training to CDN-only data path (zero HF API calls during epochs).
  - Enforce studio reuse and deterministic repo→sibling mapping for HF writes.

---

## 3. Implementation

### Step 1 — `manifest.py` (run once per date folder on Mac)
Resolves schema at generation time and records CDN URLs + content-addressing.

```python
# /opt/axentx/vanguard/manifest.py
#!/usr/bin/env python3
"""
Generate content-addressed manifest for a date folder.
Run once per date folder (or after rate-limit window clears).
"""
import json, hashlib, os, sys, pyarrow.parquet as pq
from datetime import datetime
from huggingface_hub import list_repo_tree, hf_hub_download
from io import BytesIO

REPO = os.getenv("REPO", "datasets/surrogate-1")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")  # e.g. batches/mirror-merged/2026-04-29
OUT_MANIFEST = os.getenv("OUT_MANIFEST", f"manifest-{DATE_FOLDER}.json")
PROJECT_COLS = ("prompt", "response")

def build_manifest():
    entries = []
    tree = list_repo_tree(REPO, path=DATE_FOLDER, recursive=True)
    for item in tree:
        if not item.path.endswith(".parquet"):
            continue
        cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{item.path}"
        # content-address by file bytes (stronger than path+size)
        try:
            buf = BytesIO(hf_hub_download(REPO, filename=item.path, repo_type="dataset", cache_dir=None, force_download=True, local_files_only=False))
            tbl = pq.read_table(buf, columns=PROJECT_COLS)
            projected_bytes = tbl.to_pandas().to_parquet().encode()
            digest = hashlib.sha256(projected_bytes).hexdigest()[:16]
            row_count = tbl.num_rows
        except Exception as e:
            print(f"Skipping {item.path}: {e}", file=sys.stderr)
            continue

        entries.append({
            "digest": digest,
            "path": item.path,
            "cdn_url": cdn_url,
            "size": item.size,
            "row_count": row_count,
            "date_folder": DATE_FOLDER,
            "projected_cols": list(PROJECT_COLS)
        })

    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date_folder": DATE_FOLDER,
        "repo": REPO,
        "entries": entries
    }
    with open(OUT_MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(entries)} entries to {OUT_MANIFEST}")

if __name__ == "__main__":
    build_manifest()
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/manifest.py
```

---

### Step 2 — `train.py` (CDN-only + studio reuse + sibling mapping)
Embeds manifest, uses CDN-only iterable, reuses running studio, and maps repo→sibling for HF writes.

```python
# /opt/axentx/vanguard/train.py
#!/usr/bin/env python3
import json, os, sys, requests, pyarrow.parquet as pq
from io import BytesIO
from lightning import Studio, Teamspace, Machine
from huggingface_hub import HfApi, create_repo, get_full_repo_name

# --
# CONFIG (override via env)
# --
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifest-2026-04-29.json")
STUDIO_NAME = os.getenv("STUDIO_NAME", "surrogate-1-train")
MACHINE = os.getenv("MACHINE", "L40S")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "8"))
HF_REPO_PREFIX = os.getenv("HF_REPO_PREFIX", "surrogate-1")
HF_USER = os.getenv("HF_USER", "your-hf-username")
SIBLING_MAP = {
    # date_folder -> sibling repo suffix
    "2026-04-29": "sibling-a",
    "2026-04-30": "sibling-b",
}

# --
# Load manifest
# --
if not os.path.exists(MANIFEST_PATH):
    sys.exit(f"Manifest not found: {MANIFEST_PATH}")
with open(MANIFEST_PATH) as f:
    manifest = json.load(f)

ENTRIES = manifest["entries"]
if not ENTRIES:
    sys.exit("No entries in manifest; aborting.")

# --
# CDN-only dataset iterable (zero HF API calls during training)
# --
class CDNParquetIterable:
    def __init__(self, entries, project_cols=("prompt", "response")):
        self.entries = entries
        self.project_cols = project_cols

    def __iter__(self):
        for e in self.entries:
            resp = requests.get(e["cdn_url"], timeout=30)
            resp.raise_for_status()
            tbl = pq.read_table(BytesIO(resp.content), columns=self.project_cols)
            for batch in tbl.to_batches(max_chunksize=1024):
                df = batch.to_pandas()
                for _, row in df.iterrows():
                    yield {k: row[k] for k in self.project_cols}

# --
# Lightning Studio reuse
# --
def get_or_create_studio():
    team = Teamspace()
    for s in team.studios:
        if s.name == STUDIO_NAME and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s
    print(f"Creating studio: {STUDIO_NAME}")
    return Studio(
        name=STUDIO_NAME,
        machine=Machine(MACHINE),
        create_ok=True
    )

# --
# HF sibling-aware write helper
# --
def get_hf_repo_for_date(date_folder: str) -> str:
    suffix = SIBLING_MAP.get(date_folder, "sibling-default")
    repo_name = f"{HF_REPO_PREFIX}-{suffix}"
    full_name = f"{HF_USER}/{repo_name}"
    api = HfApi()
    try:
        api.repo_info(full_name, repo_type="dataset")
    except Exception:
        print(f"Creating repo: {full_name}")
        create_repo(repo_name, repo_type="dataset", private=False, exist_ok=True)
    return full_name

# --
# Training step stub (replace with real LightningModule)
# --
def train_step(batch):
    # placeholder: implement surrogate-1 training logic here
    return {"loss": 0.0}

def run_training():
    studio = get_or_create_studio()
    dataset = CDNParquetIterable(ENTRIES)

    if studio.status != "Running":
        print("Studio not running; restarting...")
        studio.start(machine=Machine(MACHINE))

    # Example: run a small loop (replace with real training loop
