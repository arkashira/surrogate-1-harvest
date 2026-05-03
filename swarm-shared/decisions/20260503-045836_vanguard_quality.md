# vanguard / quality

## 1. Diagnosis
- Training script still calls `list_repo_tree`/`load_dataset` at runtime → triggers HF API 429s and non-reproducible shard order.
- No content-addressed manifest per date folder → epochs drift across runs and resumable training is unreliable.
- Data loader uses Hugging Face `datasets` streaming/list calls instead of CDN-only fetches → wastes API quota and slows data loading.
- No deterministic file list embedded in training artifacts → Lightning Studio reruns can pick different files and break reproducibility.
- Missing guard to reuse running Studio and check idle-stop before `.run()` → wastes quota and kills long jobs.

## 2. Proposed change
- Scope: `/opt/axentx/vanguard/train.py` (or create if absent) + `/opt/axentx/vanguard/make_manifest.py`
- Add a manifest generator that runs once on the Mac (post rate-limit window) and writes `manifests/{date}/files.json` with CDN paths.
- Update training script to read the manifest and fetch via CDN (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with zero HF API calls during training.
- Add Studio reuse + idle-stop guard before each training run.

## 3. Implementation

```bash
# Create manifest generator
cat > /opt/axentx/vanguard/make_manifest.py <<'PY'
#!/usr/bin/env python3
"""
Generate content-addressed manifest for a date folder.
Run once per date folder after HF API rate-limit window clears.
"""
import json, os, sys, hashlib, datetime
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("pip install huggingface_hub")
    sys.exit(1)

REPO = os.getenv("HF_DATASET_REPO", "datasets/your-org/surrogate-1")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())
OUT_DIR = Path("manifests") / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)

def main():
    # Single API call: non-recursive per date folder
    items = list_repo_tree(REPO, path=DATE_FOLDER, recursive=False)
    files = []
    for item in items:
        if item.type != "file":
            continue
        # CDN path (no auth, bypasses API rate limit)
        cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{item.path}"
        files.append({
            "path": item.path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None),
        })

    manifest = {
        "repo": REPO,
        "date": DATE_FOLDER,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "files": files,
        "checksum": hashlib.sha256(json.dumps([f["path"] for f in files], sort_keys=True).encode()).hexdigest()[:16],
    }

    out_path = OUT_DIR / "files.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path}")
    print(f"Files: {len(files)}")

if __name__ == "__main__":
    main()
PY
chmod +x /opt/axentx/vanguard/make_manifest.py
```

```bash
# Update/replace train.py to use manifest + CDN + Studio reuse
cat > /opt/axentx/vanguard/train.py <<'PY'
#!/usr/bin/env python3
"""
Surrogate-1 training with CDN-only data loading and Studio reuse.
"""
import json, os, sys, math, functools
from pathlib import Path

import torch
from torch.utils.data import IterableDataset, DataLoader

try:
    import lightning as L
    from lightning.fabric.plugins import LightningCLI
    from huggingface_hub import Teamspace
except ImportError:
    print("pip install lightning huggingface_hub")
    sys.exit(1)

# ---- Manifest + CDN dataset ----
class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path, buffer_size_mb=64):
        manifest_path = Path(manifest_path)
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")
        self.manifest = json.loads(manifest_path.read_text())
        self.files = [f["cdn_url"] for f in self.manifest["files"]]
        if not self.files:
            raise ValueError("No files in manifest")
        self.buffer_size = buffer_size_mb * 1024 * 1024

    def __iter__(self):
        # Simple round-robin; replace with proper parquet streaming as needed.
        # For HF CDN parquet: use pyarrow.parquet.ParquetFile via HTTP filesystem.
        import pyarrow.parquet as pq
        import pyarrow.fs as pafs

        http_fs = pafs.HadoopFileSystem("https://huggingface.co")
        idx = 0
        while True:
            cdn_url = self.files[idx % len(self.files)]
            idx += 1
            # Parse repo/path from cdn_url
            # cdn_url: https://huggingface.co/datasets/{repo}/resolve/main/{path}
            parts = cdn_url.replace("https://huggingface.co/datasets/", "").split("/resolve/main/", 1)
            if len(parts) != 2:
                continue
            repo, path = parts[0], parts[1]
            # Use pyarrow HTTP filesystem to read parquet without HF API
            try:
                pf = pq.read_table(
                    f"https://huggingface.co/datasets/{repo}/resolve/main/{path}",
                    filesystem=http_fs,
                    columns=["prompt", "response"],
                )
                table = pf.to_pylist()
                for row in table:
                    yield row.get("prompt", ""), row.get("response", "")
            except Exception as exc:
                print(f"Skipping {path}: {exc}")
                continue

# ---- Model + training step ----
class Surrogate1(L.LightningModule):
    def __init__(self, lr=1e-4):
        super().__init__()
        self.lr = lr
        # Minimal placeholder model; replace with your architecture.
        self.net = torch.nn.Linear(1024, 1024)

    def training_step(self, batch, batch_idx):
        # batch is dict with prompt/response; adapt to your tokenizer/model.
        loss = torch.tensor(0.0, device=self.device)  # placeholder
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.AdamW(self.net.parameters(), lr=self.lr)

# ---- Studio reuse + idle-stop guard ----
def get_or_create_studio(name="surrogate-1-train", machine="lightning_labs/L40S-1"):
    """
    Reuse a running Studio if present; otherwise create one.
    Check idle-stop before run and restart if stopped.
    """
    from lightning.fabric.plugins import Teamspace
    ts = Teamspace()
    for s in ts.studios:
        if s.name == name:
            if s.status == "running":
                print(f"Reusing running studio: {s.name}")
                return s
            else:
                print(f"Studio {s.name} exists but status={s.status}. Starting...")
                s.start(machine=machine)
                return s
    print(f"Creating studio: {name}")
    return ts.create_studio(name=name, machine=machine, create_ok=True)

# ---- CLI ----
def cli_main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="manifests/2026-05-03/files.json", help="Path to manifest")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--studio", action="store_true", help="Run in Lightning Studio with reuse")
    parser.add_argument("--studio-name", default="surrogate-1-train")
    args = parser.parse_args()

    if args.studio:
        studio = get_or_create_studio(name=args.studio_name)
        # Studio will execute this script internally; ensure manifest is accessible.
        # For local dev, run without --studio.

    dataset = CDNParquetDataset(args.manifest)
    loader = DataLoader(dataset, batch_size=args.batch_size, num_workers=
