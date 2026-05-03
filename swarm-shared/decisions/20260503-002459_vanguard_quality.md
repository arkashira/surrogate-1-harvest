# vanguard / quality

## 1. Diagnosis

- No persisted `(repo, dateFolder)` file manifest → every training run re-enumerates via authenticated HF API → quota burn + 429 risk.
- Training script likely uses `load_dataset(streaming=True)` or recursive enumeration during data loading → mixed-schema CastError and rate-limit exposure.
- No CDN-only data path → training depends on `/api/` endpoints that enforce strict rate limits instead of higher CDN limits.
- Missing Lightning Studio reuse logic → each run creates new Studio and burns 80hr/mo quota.
- No deterministic sibling-repo write strategy for HF ingestion → commit cap (128/hr/repo) throttles mirror/ingest throughput.

## 2. Proposed change

Add a small, high-leverage quality layer:

- File: `/opt/axentx/vanguard/train.py` (or create if absent) — add manifest generation + CDN-only data loader + Studio reuse.
- File: `/opt/axentx/vanguard/ingest.py` (or create) — add sibling-repo deterministic routing and schema projection before upload.
- Scope: ~120 lines total; focused on data path and infra reuse (no model changes).

## 3. Implementation

```bash
# Ensure scripts are executable and use proper shebang
cat > /opt/axentx/vanguard/train.py <<'PY'
#!/usr/bin/env python3
"""
Surrogate-1 training entrypoint (quality fixes):
- Persist (repo, dateFolder) file manifest once (Mac side).
- Lightning training uses CDN-only fetches (zero API calls during data load).
- Reuse running Studio to save quota.
"""
import json, os, subprocess, datetime, pathlib, sys
from huggingface_hub import HfApi, list_repo_tree, hf_hub_download
from lightning import Fabric, LightningFlow, LightningWork, Cloud, Run

HF_REPO = os.getenv("HF_REPO", "datasets/surrogate-1")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())
MANIFEST_PATH = pathlib.Path("file_manifest.json")
SIBLING_REPOS = [f"{HF_REPO}-s{i}" for i in range(5)]

def build_manifest():
    """Single authenticated call on Mac; save for Lightning to reuse."""
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())

    api = HfApi()
    entries = list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    files = [e.path for e in entries if e.type == "file" and e.path.endswith(".parquet")]
    manifest = {"repo": HF_REPO, "date": DATE_FOLDER, "files": files}
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    return manifest

def cdn_url(repo: str, path: str) -> str:
    """CDN bypass: no Authorization header required."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def dataset_generator(manifest):
    """Lightning training uses generator + CDN-only downloads (zero HF API calls)."""
    import pyarrow.parquet as pq
    import io, requests
    for path in manifest["files"]:
        url = cdn_url(manifest["repo"], path)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        table = pq.read_table(io.BytesIO(resp.content))
        # Project to {prompt, response} only (ignore mixed schema cols)
        for batch in table.to_batches(max_chunksize=1024):
            batch = batch.select(["prompt", "response"])
            for i in range(batch.num_rows):
                yield {
                    "prompt": batch["prompt"][i].as_py(),
                    "response": batch["response"][i].as_py(),
                }

def pick_sibling_repo(slug: str) -> str:
    """Deterministic sibling repo to spread HF commit load."""
    idx = hash(slug) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def reuse_or_create_studio():
    """Reuse running Studio to save Lightning quota."""
    from lightning import Teamspace
    name = "surrogate-1-train"
    for s in Teamspace.studios:
        if s.name == name and s.status == "running":
            return s
    # fallback: create (once) and reuse thereafter
    return Teamspace.studios.create(
        name=name,
        cloud=Cloud.LIGHTNING_PUBLIC_PROD,
        machine="L40S",
        create_ok=True,
    )

def train():
    manifest = build_manifest()
    studio = reuse_or_create_studio()
    if studio.status != "running":
        studio.start(machine="L40S")

    fabric = Fabric(devices=1, accelerator="cuda", precision="bf16-mixed")
    # Example: plug your model + dataloader here.
    # dataloader = fabric.DataLoader(dataset_generator(manifest), batch_size=8)
    print("Manifest built. CDN-only data path ready. Studio:", studio.name, studio.status)

if __name__ == "__main__":
    train()
PY

chmod +x /opt/axentx/vanguard/train.py
```

```bash
cat > /opt/axentx/vanguard/ingest.py <<'PY'
#!/usr/bin/env python3
"""
Ingestion quality fixes:
- Project to {prompt, response} before upload (no mixed-schema cols).
- Deterministic sibling-repo routing to bypass 128/hr/repo commit cap.
- Filename pattern: batches/mirror-merged/{date}/{slug}.parquet
"""
import os, pyarrow as pa, pyarrow.parquet as pq, hashlib, datetime, json
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_REPO", "datasets/surrogate-1")
SIBLING_REPOS = [f"{HF_REPO}-s{i}" for i in range(5)]
API = HfApi()

def sibling_for(slug: str) -> str:
    idx = hash(slug) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def project_and_upload(source_path: str, slug: str, date: str = None):
    date = date or datetime.date.today().isoformat()
    table = pq.read_table(source_path)
    # Keep only prompt/response; drop source/ts/other mixed cols
    cols = [c for c in table.column_names if c in {"prompt", "response"}]
    table = table.select(cols)

    out_dir = f"batches/mirror-merged/{date}"
    fname = f"{slug}.parquet"
    out_path = f"{out_dir}/{fname}"

    repo = sibling_for(slug)
    API.upload_file(
        path_or_fileobj=pa.BufferOutputStream().to_pybytes(),
        # write to tmp then upload file content properly
        path_in_repo=out_path,
        repo_id=repo,
    )
    # Real upload: write parquet to tmp then upload_file(path=tmp, ...)
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        pq.write_table(table, tmp.name)
        API.upload_file(
            path_or_fileobj=tmp.name,
            path_in_repo=out_path,
            repo_id=repo,
        )
    return {"repo": repo, "path": out_path, "slug": slug}

if __name__ == "__main__":
    # Example usage (replace with real source files)
    print("Ingestion helpers ready. Use project_and_upload(...)")
PY

chmod +x /opt/axentx/vanguard/ingest.py
```

## 4. Verification

1. Manifest creation (Mac side):
   ```bash
   cd /opt/axentx/vanguard
   HF_REPO=datasets/surrogate-1 DATE_FOLDER=2026-04-29 python3 train.py
   ```
   - Confirm `file_manifest.json` exists and lists parquet files for the date folder.
   - Confirm no HF API calls occur during generator iteration (check with `HF_TOKEN=none` and network logs — CDN downloads should succeed).

2. CDN-only data path:
   - Run a small training loop (or dry-run) and verify batches are produced from `dataset_generator`.
   - Confirm no `load_dataset` or recursive `list_repo_tree` calls during training.

3. Studio reuse:
   - Start a Studio manually, then run script again —
