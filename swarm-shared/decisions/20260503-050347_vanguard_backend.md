# vanguard / backend

## 1. Diagnosis

- Training pipeline still resolves data via HF `datasets` API at runtime (`list_repo_tree` / `load_dataset`) → exposes surrogate-1 to 429 rate limits and non-reproducible shard order.
- No content-addressed manifest per date folder → epochs drift across runs and resumable training is unreliable.
- Data loader performs schema projection at runtime (mixed schemas) → `pyarrow.CastError` risk and slower epochs.
- No local file-list cache → every run hits HF API even for same date folder; CDN bypass is unused.
- Lightning Studio reuse is fragile (idle stop kills training) and no pre-check before `.run()` → quota waste and failed jobs.

## 2. Proposed change

Create a backend manifest generator + training-side CDN-only loader:

- `/opt/axentx/vanguard/backend/manifest.py` — scans a date folder via single `list_repo_tree` call, produces `manifest-{date}.json` with content-addressed entries `{sha256, path, size, url}`.
- `/opt/axentx/vanguard/backend/dataloader.py` — reads manifest, streams files via HF CDN URLs (no auth, no API), projects to `{prompt, response}` only.
- Patch training script to accept `--manifest` and skip any `datasets`/`list_repo_tree` calls during training.
- Add lightweight Studio lifecycle guard (`ensure_studio_running`) that reuses or restarts a named studio before training.

## 3. Implementation

```bash
# create backend module
mkdir -p /opt/axentx/vanguard/backend
```

```python
# /opt/axentx/vanguard/backend/manifest.py
import json, hashlib, os
from datetime import datetime
from huggingface_hub import HfApi, hf_hub_download

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/surrogate-1")
API = HfApi()

def list_date_folder(date_folder: str, repo: str = HF_REPO):
    """Single API call: non-recursive per date folder."""
    items = API.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    return [i for i in items if i.type == "file"]

def build_manifest(date_folder: str, out_dir: str = "manifests"):
    os.makedirs(out_dir, exist_ok=True)
    files = list_date_folder(date_folder)
    entries = []
    for f in files:
        # CDN URL bypasses API auth/rate limits
        cdn_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{date_folder}/{f.path}"
        # content-address by path+size (cheap; avoids extra download)
        digest = hashlib.sha256(f"{date_folder}/{f.path}{f.size}".encode()).hexdigest()
        entries.append({
            "sha256": digest,
            "path": f"{date_folder}/{f.path}",
            "size": f.size,
            "url": cdn_url
        })
    manifest = {
        "date_folder": date_folder,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "repo": HF_REPO,
        "entries": entries
    }
    out_path = os.path.join(out_dir, f"manifest-{date_folder.replace('/', '-')}.json")
    with open(out_path, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Wrote {out_path} ({len(entries)} files)")
    return out_path

if __name__ == "__main__":
    import sys
    date_folder = sys.argv[1] if len(sys.argv) > 1 else "batches/mirror-merged/2026-04-29"
    build_manifest(date_folder)
```

```python
# /opt/axentx/vanguard/backend/dataloader.py
import json, io, pyarrow as pa, pyarrow.parquet as pq
import requests
from typing import Iterator, Tuple

def project_to_pair(batch_bytes: bytes) -> Tuple[str, str]:
    """Extract (prompt, response) from parquet bytes; ignore other columns."""
    table = pq.read_table(io.BytesIO(batch_bytes), columns=["prompt", "response"])
    # coerce to string; drop nulls
    prompts = table.column("prompt").to_pylist()
    responses = table.column("response").to_pylist()
    pairs = [(str(p) if p is not None else "", str(r) if r is not None else "")
             for p, r in zip(prompts, responses)]
    return [p for p, _ in pairs], [r for _, r in pairs]

def stream_cdn_parquet(entries, batch_size: int = 32) -> Iterator[Tuple[list, list]]:
    """Yield (prompts, responses) batches via CDN URLs (no HF API calls)."""
    batch_paths = []
    for i, entry in enumerate(entries):
        batch_paths.append(entry["url"])
        if len(batch_paths) == batch_size or i == len(entries) - 1:
            all_prompts, all_responses = [], []
            for url in batch_paths:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                prompts, responses = project_to_pair(resp.content)
                all_prompts.extend(prompts)
                all_responses.extend(responses)
            yield all_prompts, all_responses
            batch_paths = []

def load_manifest(manifest_path: str):
    with open(manifest_path) as fp:
        return json.load(fp)
```

```python
# /opt/axentx/vanguard/backend/studio.py
import os
from lightning import Lightning, Teamspace, Machine

LIGHTNING = Lightning()
TEAMSPACE = Teamspace()

def ensure_studio_running(studio_name: str, machine: str = "L40S"):
    for s in TEAMSPACE.studios:
        if s.name == studio_name:
            if s.status == "Running":
                print(f"Reusing running studio: {studio_name}")
                return s
            else:
                print(f"Restarting stopped studio: {studio_name}")
                s.start(machine=Machine(machine))
                return s
    print(f"Creating studio: {studio_name}")
    return LIGHTNING.Studio(
        name=studio_name,
        machine=machine,
        create_ok=True
    )
```

```python
# /opt/axentx/vanguard/train.py  (minimal patch example)
import argparse, os
from backend.dataloader import load_manifest, stream_cdn_parquet
from backend.studio import ensure_studio_running

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="path to manifest-*.json")
    parser.add_argument("--studio", default="surrogate-1-train")
    args = parser.parse_args()

    # optional: ensure studio alive before heavy work
    ensure_studio_running(args.studio, machine=os.getenv("LIGHTNING_MACHINE", "L40S"))

    manifest = load_manifest(args.manifest)
    print(f"Loaded {len(manifest['entries'])} files from {manifest['date_folder']}")

    # CDN-only data stream — zero HF API calls during training
    for step, (prompts, responses) in enumerate(stream_cdn_parquet(manifest["entries"], batch_size=64)):
        # your training step here (e.g., tokenize + forward/backward)
        # example: train_on_texts(prompts, responses)
        if step % 10 == 0:
            print(f"step {step}: {len(prompts)} samples")

if __name__ == "__main__":
    main()
```

## 4. Verification

1. Generate manifest (run once per date folder from your Mac — safe, single API call):
   ```bash
   cd /opt/axentx/vanguard
   python backend/manifest.py batches/mirror-merged/2026-04-29
   # -> manifests/manifest-batches-mirror-merged-2026-04-29.json
   ```

2. Confirm CDN-only streaming (no auth headers, no HF API):
   ```bash
   python -c "
from backend.dataloader import load_manifest, stream_cdn_parquet
m = load_manifest('manifests/manifest-batches-mirror-merged-2026-04-29.json')
for prompts, responses in stream_cdn_parquet(m['entries'], batch_size=4):
    print('batch:',
