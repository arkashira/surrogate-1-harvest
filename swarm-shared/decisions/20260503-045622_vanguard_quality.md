# vanguard / quality

## Final Synthesis (Best of Both Candidates)

**Diagnosis (merged, de-duplicated, corrected)**
- Training script calls `list_repo_tree`/`load_dataset` at runtime → exposes training to HF API 429s, wastes quota, and produces non-reproducible shard order.
- No content-addressed manifest per date folder → epochs drift and resumable training is unreliable.
- Data loader uses HF `datasets` streaming/list calls instead of CDN-only fetches → fails under rate limits.
- Missing deterministic repo-to-slug mapping for commit-cap mitigation (128/hr/repo) → ingestion bursts can be blocked.
- No Lightning Studio reuse guard before `.run()` → idle-stop kills training and burns quota.

**Proposed Change**
File: `/opt/axentx/vanguard/train.py` (update) + `/opt/axentx/vanguard/build_manifest.py` (create)  
Scope: replace HF streaming/list calls with a manifest-driven, CDN-only data loader; add deterministic repo routing; add Studio reuse; add manifest builder run on the Mac orchestrator once per date folder.

---

## Implementation

### Step 1 — Manifest builder (run on Mac, once per date folder)
```python
# /opt/axentx/vanguard/build_manifest.py
#!/usr/bin/env python3
"""
Generate content-addressed manifest for a date folder.
Run from Mac after rate-limit window clears.
"""
import json, hashlib, os, sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/surrogate-1")
DATE_FOLDER = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
OUT_PATH = os.path.join("manifests", f"{DATE_FOLDER}.json")

def deterministic_repo(slug: str, n_siblings: int = 5) -> str:
    """Map slug -> sibling repo deterministically to spread commit cap."""
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    idx = h % n_siblings
    if idx == 0:
        return HF_REPO
    return f"{HF_REPO}-sibling-{idx}"

def main():
    api = HfApi()
    # non-recursive to avoid pagination explosion; use repo_id from deterministic mapping root
    files = api.list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    entries = []
    for f in files:
        if not getattr(f, "path", "").endswith(".parquet"):
            continue
        repo = deterministic_repo(f.path)
        entries.append({
            "repo": repo,
            "path": f.path,
            "url": f"https://huggingface.co/datasets/{repo}/resolve/main/{f.path}",
            "size": getattr(f, "size", None),
            "etag": getattr(f, "etag", None)
        })

    manifest = {
        "date": DATE_FOLDER,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Wrote {len(entries)} entries to {OUT_PATH}")

if __name__ == "__main__":
    main()
```

Make executable and ensure cron uses bash:
```bash
chmod +x /opt/axentx/vanguard/build_manifest.py
# In crontab (if used):
SHELL=/bin/bash
```

---

### Step 2 — Update training script to use manifest + CDN-only + Studio reuse
```python
# /opt/axentx/vanguard/train.py
#!/usr/bin/env python3
"""
Surrogate-1 training: manifest-driven, CDN-only dataset loading.
"""
import json, os, sys
from pathlib import Path
from typing import Dict

import torch
from datasets import load_dataset, Features, Value
from lightning import Fabric

# Optional: Lightning Studio reuse
try:
    from lightning import Studio, Teamspace, Machine
    _STUDIO_AVAILABLE = True
except Exception:
    _STUDIO_AVAILABLE = False
    Studio = Teamspace = Machine = None

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifests/2026-04-29.json")

def load_manifest(path: str) -> Dict:
    with open(path) as f:
        return json.load(f)

def cdn_only_dataset(manifest: Dict, streaming: bool = False):
    """
    Build dataset by streaming from CDN URLs (no HF API calls during training).
    Uses `load_dataset` with `data_files` pointing to raw CDN URLs.
    """
    entries = manifest["entries"]
    if not entries:
        raise ValueError("No entries in manifest")

    data_files = [e["url"] for e in entries]
    features = Features({
        "prompt": Value("string"),
        "response": Value("string"),
    })

    ds = load_dataset(
        "parquet",
        name="surrogate",
        data_files=data_files,
        features=features,
        split="train",
        streaming=streaming,
    )
    return ds

def reuse_or_create_studio(name: str = "surrogate-1-train", machine: str = "L40S"):
    """Reuse running studio to save quota; restart if idle-stopped."""
    if not _STUDIO_AVAILABLE:
        return None
    try:
        teamspace = Teamspace()
        for s in teamspace.studios:
            if s.name == name:
                if s.status == "Running":
                    print(f"Reusing running studio: {name}")
                    return s
                print(f"Studio {name} exists but stopped. Restarting...")
                target_machine = Machine.L40S
                s.start(machine=target_machine)
                return s
        print(f"Creating studio {name}")
        return Studio(name=name, machine=machine, create_ok=True)
    except Exception as exc:
        print(f"Studio reuse failed ({exc}). Continuing without studio.")
        return None

def train_step(batch, model, fabric):
    # Minimal training step placeholder
    # Replace with real tokenization and loss
    return {"loss": torch.tensor(0.0, device=fabric.device)}

def main():
    manifest = load_manifest(MANIFEST_PATH)
    print(f"Loaded manifest for {manifest['date']} with {len(manifest['entries'])} files")

    # Optional: reuse studio
    _ = reuse_or_create_studio()

    # CDN-only dataset (set streaming=True for very large datasets)
    dataset = cdn_only_dataset(manifest, streaming=False)

    # Fabric setup
    fabric = Fabric(accelerator="cuda", devices=1, precision="bf16-mixed")
    fabric.launch()

    # Dummy model for illustration
    model = torch.nn.Linear(10, 10)
    model = fabric.setup_module(model)

    # DataLoader (if streaming=True, use iterable-style loop instead)
    loader = torch.utils.data.DataLoader(dataset, batch_size=8, shuffle=True, num_workers=0)
    loader = fabric.setup_dataloaders(loader)

    # Train loop (minimal)
    model.train()
    for batch in loader:
        out = train_step(batch, model, fabric)
        fabric.print(out)
        break

    print("Training step completed (manifest-driven, CDN-only).")

if __name__ == "__main__":
    main()
```

Make executable and ensure Bash for any wrapper/cron:
```bash
chmod +x /opt/axentx/vanguard/train.py
```

If you wrap this in cron or systemd, set:
```
SHELL=/bin/bash
```

---

## Verification
1. Run manifest builder once:
   ```bash
   cd /opt/axentx/vanguard
   python3 build_manifest.py 2026-04-29
   ```
   Confirm `manifests/2026-04-29.json` exists and contains CDN `url` fields.

2. Dry-run training (small test):
   ```bash
   MANIFEST_PATH=manifests/2026-04-29.json python3 train.py
   ```
   Expect: manifest load message, dataset creation from CDN URLs, one training step printed, and completion message. No HF
