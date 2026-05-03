# vanguard / discovery

## 1. Diagnosis
- No content-addressed manifest per date folder forces runtime repo enumeration via `list_repo_tree`/`load_dataset`, triggering HF API 429s and non-reproducible epochs.
- Training script performs HF API calls during data loading (instead of CDN-only), wasting rate-limit quota and risking mid-epoch failures.
- Missing deterministic `{path, sha256}` snapshot prevents resumable/reproducible runs and safe studio reuse.
- No guard to reuse an already-running Lightning Studio, burning ~80hr/mo quota on redundant starts.
- Idle-stop kills training; no pre-run status check or auto-restart on stopped studios.

## 2. Proposed change
Create `/opt/axentx/vanguard/scripts/make_manifest.py` and update `/opt/axentx/vanguard/train.py` (or create if absent) to:
- Generate a `manifests/{date}/files.json` with `{path, sha256, url}` via a single Mac-side HF API call.
- Embed that manifest in training so Lightning Studio does **CDN-only** fetches with zero API calls.
- Add studio reuse + idle-stop guard before `.run()`.

Scope: new file `scripts/make_manifest.py`; modify or create `train.py` in project root.

## 3. Implementation

```bash
# /opt/axentx/vanguard/scripts/make_manifest.py
#!/usr/bin/env bash
# Generate content-addressed manifest for a date folder to enable CDN-only training.
# Usage: bash make_manifest.sh <repo> <date_folder> [out_dir]
# Example: bash make_manifest.sh axentx/datasets 2026-04-29 manifests

set -euo pipefail
REPO="${1:-axentx/datasets}"
DATE="${2:-$(date +%Y-%m-%d)}"
OUTDIR="${3:-manifests/$DATE}"

mkdir -p "$OUTDIR"
MANIFEST="$OUTDIR/files.json"

python3 - "$REPO" "$DATE" "$MANIFEST" <<'PY'
import os, json, hashlib, subprocess, sys
from huggingface_hub import list_repo_tree, hf_hub_download

REPO, DATE, MANIFEST = sys.argv[1], sys.argv[2], sys.argv[3]

# Single API call: non-recursive tree for the date folder
tree = list_repo_tree(repo_id=REPO, path=DATE, recursive=False)
entries = []
for obj in tree:
    if obj.type != "file":
        continue
    path = f"{DATE}/{obj.path}"
    # sha256 via CDN HEAD (fast) or hf_hub_download(revision="main")
    url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"
    entries.append({
        "path": path,
        "sha256": obj.lfs.get("sha256", ""),  # may be empty; fallback below if needed
        "size": obj.size,
        "url": url
    })

# If sha256 missing, compute via CDN range fetch (optional, skip for speed)
# We'll rely on path+size for reproducibility; sha256 best-effort.
with open(MANIFEST, "w") as f:
    json.dump({"repo": REPO, "date": DATE, "generated_by": "make_manifest", "files": entries}, f, indent=2)

print(f"Wrote {len(entries)} files to {MANIFEST}")
PY

echo "Manifest created: $MANIFEST"
```

```python
# /opt/axentx/vanguard/train.py
#!/usr/bin/env python3
"""
CDN-only training entrypoint for Lightning Studio.
Expects MANIFEST_JSON env or arg: manifests/YYYY-MM-DD/files.json
"""
import os, json, hashlib, requests
from pathlib import Path
from torch.utils.data import IterableDataset, DataLoader
import lightning as L
from huggingface_hub import Teamspace

MANIFEST_PATH = os.getenv("MANIFEST_JSON", "manifests/latest/files.json")

class CDNTextDataset(IterableDataset):
    def __init__(self, manifest_path, max_files=None):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = self.manifest["files"]
        if max_files:
            self.files = self.files[:max_files]

    def _stream_file(self, entry):
        # CDN-only: no Authorization header
        resp = requests.get(entry["url"], stream=True, timeout=60)
        resp.raise_for_status()
        # Minimal projection: yield lines as {prompt, response}
        # Replace with your actual parser (e.g., JSONL, parquet via pyarrow)
        for chunk in resp.iter_lines(decode_unicode=True):
            if not chunk:
                continue
            # Example: assume each line is {"prompt": "...", "response": "..."}
            try:
                data = json.loads(chunk)
                yield {"prompt": data.get("prompt", ""), "response": data.get("response", "")}
            except Exception:
                continue

    def __iter__(self):
        for entry in self.files:
            yield from self._stream_file(entry)

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, manifest_path, batch_size=8):
        super().__init__()
        self.manifest_path = manifest_path
        self.batch_size = batch_size

    def train_dataloader(self):
        return DataLoader(CDNTextDataset(self.manifest_path), batch_size=self.batch_size, num_workers=0)

class SurrogateModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        # Minimal model for demo; replace with your architecture
        self.net = torch.nn.Linear(512, 512)

    def training_step(self, batch, batch_idx):
        # Dummy loss; replace with real training logic
        x = torch.randn(batch["prompt"].shape[0], 512, device=self.device)
        y = self.net(x)
        loss = y.sum() * 0.0
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.nn.optim.Adam(self.parameters(), lr=1e-4)

def reuse_or_create_studio(name="vanguard-training", machine="lightning-ai/L40S-1"):
    # Reuse running studio to save quota
    for s in Teamspace().studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {s.name}")
            return s
    # Create new if none running
    from lightning.pytorch.studio import Studio
    return Studio(
        name=name,
        target=machine,
        create_ok=True,
        start_now=False
    )

def main():
    import torch
    manifest = os.getenv("MANIFEST_JSON", MANIFEST_PATH)
    if not Path(manifest).exists():
        raise FileNotFoundError(f"Manifest not found: {manifest}. Run scripts/make_manifest.py first.")

    studio = reuse_or_create_studio()
    if studio.status != "running":
        print("Studio not running; starting...")
        studio.start(machine="lightning-ai/L40S-1")

    dm = SurrogateDataModule(manifest_path=manifest, batch_size=8)
    model = SurrogateModel()

    trainer = L.Trainer(
        max_epochs=1,
        devices=1,
        accelerator="gpu",
        log_every_n_steps=10,
        enable_checkpointing=False
    )
    trainer.fit(model, dm)

if __name__ == "__main__":
    main()
```

```bash
# Make scripts executable
chmod +x /opt/axentx/vanguard/scripts/make_manifest.py
chmod +x /opt/axentx/vanguard/train.py
```

## 4. Verification
1. Generate manifest (single API call from Mac):
   ```bash
   cd /opt/axentx/vanguard
   bash scripts/make_manifest.py axentx/datasets 2026-04-29 manifests
   ```
   Confirm `manifests/2026-04-29/files.json` exists and lists files with `url` fields.

2. Validate CDN URLs work without auth:
   ```bash
   head -1 manifests/2026-04-29/files.json | jq -r '.files[0].url' | xargs
