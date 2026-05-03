# vanguard / quality

## Final Synthesis & Action Plan

**Core diagnosis (agreed by both):**
- Authenticated `list_repo_tree` is called repeatedly (per session / per training run) instead of once per `(repo, dateFolder)`, burning HF API quota (1000/5min) and causing 429s.
- No persisted manifest; each reload re-enumerates folders and re-downloads metadata.
- Training does not use a static file list, preventing reliable CDN-only data loading and increasing failure surface.
- No guardrails to reuse running Lightning Studio; jobs can silently die or waste quota by recreating studios.
- Missing explicit CDN-only data path strategy (resolve/main URLs) with zero authenticated calls during training.

**Chosen strategy (merged strongest points, resolved contradictions):**
- Generate a **single, versioned manifest per `(repo, dateFolder)`** that contains only CDN URLs (`resolve/main/...`). This is the source of truth for training and guarantees zero HF API calls during data loading.
- **Fail fast** if the manifest is missing or stale; never fall back to runtime `list_repo_tree` during training.
- **Reuse running Lightning Studio** by name and status; refuse to recreate if already running. Prefer L40S with explicit cloud priority (lambda-prod → public-prod) and surface clear errors if unavailable.
- Keep implementation minimal (~120 LoC), safe to land quickly, and fully testable on a Mac before deploying to cloud.

---

## 1. Implementation (merged + hardened)

```bash
# /opt/axentx/vanguard/scripts/build_manifest.py
#!/usr/bin/env python3
"""
Generate a CDN-only manifest for (repo, dateFolder).

Usage:
  python3 scripts/build_manifest.py \
    --repo datasets/axentx/surrogate-1 \
    --date 2026-04-29 \
    --out manifests/
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("pip install huggingface_hub")
    sys.exit(1)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="HF repo id (e.g. datasets/axentx/surrogate-1)")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder under repo root")
    parser.add_argument("--out", default="manifests", help="Output directory")
    args = parser.parse_args()

    api = HfApi()
    prefix = f"{args.date}/"
    entries = api.list_repo_tree(repo_id=args.repo, path=prefix, recursive=True)

    files = []
    for e in entries:
        if not e.path.endswith(".parquet"):
            continue
        cdn_url = f"https://huggingface.co/datasets/{args.repo}/resolve/main/{e.path}"
        files.append({"path": e.path, "url": cdn_url})

    if not files:
        print(f"No parquet files found under {args.repo}/{prefix}")
        sys.exit(1)

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_repo = args.repo.replace("/", "__")
    out_path = out_dir / f"{safe_repo}__{args.date}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files -> {out_path}")

if __name__ == "__main__":
    main()
```

```python
# /opt/axentx/vanguard/train.py
"""
Train with CDN-only parquet files listed in a manifest.
Zero authenticated HF calls during data loading.

Usage:
  python3 train.py --manifest manifests/datasets__axentx__surrogate-1__2026-04-29.json
"""
import argparse
import json
from pathlib import Path

import torch
from torch.utils.data import DataLoader, Dataset
from lightning import LightningModule, Trainer

class CDNParquetDataset(Dataset):
    def __init__(self, manifest_path: str):
        manifest = json.loads(Path(manifest_path).read_text())
        self.urls = [f["url"] for f in manifest["files"]]
        if not self.urls:
            raise ValueError("No parquet URLs in manifest")

    def __len__(self):
        return len(self.urls)

    def __getitem__(self, idx):
        # Stream row-groups via CDN without HF API.
        import pyarrow.parquet as pq
        import fsspec

        url = self.urls[idx]
        with fsspec.open(url, "rb") as f:
            table = pq.read_table(f)
        rec = table.select(["prompt", "response"]).to_pylist()[0]
        # Minimal projection; adapt to your schema.
        return torch.tensor(rec["prompt"], dtype=torch.float32), torch.tensor(rec["response"], dtype=torch.float32)

class SurrogateModule(LightningModule):
    def __init__(self, dim: int = 1024):
        super().__init__()
        self.lm = torch.nn.Linear(dim, dim)

    def training_step(self, batch, batch_idx):
        x, y = batch
        loss = torch.nn.functional.mse_loss(self.lm(x), y)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-epochs", type=int, default=1)
    args = parser.parse_args()

    ds = CDNParquetDataset(args.manifest)
    loader = DataLoader(ds, batch_size=args.batch_size, num_workers=0)

    model = SurrogateModule()
    trainer = Trainer(max_epochs=args.max_epochs, accelerator="gpu", devices=1)
    trainer.fit(model, loader)

if __name__ == "__main__":
    main()
```

```python
# /opt/axentx/vanguard/launch_studio.py
#!/usr/bin/env python3
"""
Reuse running Lightning Studio or start L40S (cloud priority: lambda-prod -> public-prod).
Never recreate a running studio with the same name.
"""
import sys

try:
    from lightning import Studio, Teamspace, Machine
except ImportError:
    print("pip install lightning")
    sys.exit(1)

def get_or_start_studio(name: str = "vanguard-train") -> Studio:
    team = Teamspace.current()
    for s in team.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s

    print(f"Starting new studio: {name}")
    clouds = ["lightning-lambda-prod", "lightning-public-prod"]
    for cloud in clouds:
        try:
            return Studio(
                name=name,
                machine=Machine.L40S,
                cloud=cloud,
                create_ok=True,
            )
        except Exception as exc:
            print(f"Cloud {cloud} failed: {exc}")
            continue
    raise RuntimeError("No available cloud for studio")

def main():
    studio = get_or_start_studio()
    print(f"Studio ready: {studio.name} ({studio.status})")

if __name__ == "__main__":
    main()
```

```bash
# Make helpers executable
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
chmod +x /opt/axentx/vanguard/launch_studio.py
```

---

## 2. Verification (concrete steps)

1. **Generate manifest once per `(repo, dateFolder)`** (run on Mac after HF window clears):
   ```bash
   cd /opt/axentx/vanguard
   python3 scripts/build_manifest.py \
     --repo datasets/axentx/surrogate-1 \
     --date 2026-04-29 \
     --out manifests
   ```
   - Expect: `manifests/dat
