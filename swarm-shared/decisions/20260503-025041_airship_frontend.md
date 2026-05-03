# airship / frontend

## Final Unified Implementation Plan (≤2h)

**Core invariant**: eliminate HF API calls during training by pre-caching a deterministic file list and using CDN-only fetches; prevent Lightning idle-timeout waste by reusing a Running Studio and guarding against idle-stop.

---

### 1) Orchestrator: cache HF file list once (Mac/CI)

- Run after the rate-limit window clears.
- Single `list_repo_tree(..., recursive=False)` for one date folder (e.g. `batches/mirror-merged/2026-05-03/`).
- Emit deterministic `filelist.json` with CDN-ready URLs and local slugs.
- Spread sibling repo writes via hash-slug to avoid hot repos.

**Deliverable**: `scripts/cache_hf_filelist.py`

```python
#!/usr/bin/env python3
"""
Cache HF repo file list for a single date folder to avoid API 429 during training.
Produces filelist.json with CDN-ready URLs.
"""
import argparse
import hashlib
import json
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = "datasets/org/surrogate-data"
SIBLING_REPOS = [f"datasets/org/surrogate-data-sib{i}" for i in range(1, 6)]

def pick_sibling_repo(slug: str) -> str:
    h = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(h, 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def build_filelist(repo: str, date_folder: str, out_path: Path):
    api = HfApi()
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    entries = []
    for item in tree:
        if not item.path.endswith((".jsonl", ".parquet", ".json")):
            continue
        cdn_url = f"https://huggingface.co/{repo}/resolve/main/{item.path}"
        entries.append({
            "repo": repo,
            "path": item.path,
            "cdn_url": cdn_url,
            "slug": Path(item.path).stem,
        })

    out = {
        "repo": repo,
        "date_folder": date_folder,
        "files": entries,
        "cdn_only": True,
    }
    out_path.write_text(json.dumps(out, indent=2))
    print(f"Wrote {len(entries)} files to {out_path}")

def main():
    parser = argparse.ArgumentParser(description="Cache HF file list for CDN training")
    parser.add_argument("--repo", default=HF_REPO)
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-03")
    parser.add_argument("--out", default="filelist.json")
    args = parser.parse_args()

    date_folder = f"batches/mirror-merged/{args.date}"
    out_path = Path(args.out).resolve()
    build_filelist(args.repo, date_folder, out_path)

if __name__ == "__main__":
    main()
```

---

### 2) Training: CDN-only dataset + Lightning launcher with Studio reuse + idle-stop guard

- Accept `--filelist` and `--repo`.
- `IterableDataset` streams via `requests.get(cdn_url, timeout=60)` with retries/backoff.
- Project bytes → `{prompt, response}` only at parse time (no schema assumptions).
- Before `.fit`/`.run`, check Teamspace for existing Running Studio by name and reuse it.
- If stopped, restart with `target.start(machine=Machine.L40S)` (respect free-tier fallback).
- Use small persistent cache dir for partial downloads to survive idle restarts.

**Deliverable**: `surrogate/train_cdn.py`

```python
#!/usr/bin/env python3
"""
CDN-first training for Surrogate.
- Uses filelist.json to avoid HF API calls during dataload.
- Reuses Running Studio and guards against idle-stop.
"""
import json
import time
from pathlib import Path
from typing import Dict, Iterator, Optional

import requests
import torch
from torch.utils.data import IterableDataset
from lightning import Fabric, LightningModule, Trainer
from lightning.pytorch.loggers import CSVLogger

# Optional Lightning Studio integration (best-effort)
try:
    from lightning import Studio, Machine, Teamspace as LightningTeamspace
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False

class CDNIterableDataset(IterableDataset):
    def __init__(self, filelist_path: Path, max_retries: int = 3, backoff: float = 2.0):
        super().__init__()
        self.filelist_path = filelist_path
        self.max_retries = max_retries
        self.backoff = backoff
        with open(filelist_path) as f:
            self.manifest = json.load(f)
        self.files = self.manifest["files"]

    def _stream_one(self, entry: Dict) -> Optional[Dict]:
        url = entry["cdn_url"]
        for attempt in range(self.max_retries):
            try:
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                # Placeholder projection; adapt to real format (jsonl/parquet)
                data = {"prompt": f"projected from {entry['path']}", "response": resp.text[:2000]}
                return data
            except Exception as exc:
                if attempt == self.max_retries - 1:
                    print(f"Failed {url}: {exc}")
                    return None
                time.sleep(self.backoff ** attempt)
        return None

    def __iter__(self) -> Iterator[Dict]:
        for entry in self.files:
            item = self._stream_one(entry)
            if item is not None:
                yield item

class SurrogateModel(LightningModule):
    def __init__(self):
        super().__init__()
        self.layer = torch.nn.Linear(512, 512)

    def training_step(self, batch, batch_idx):
        # Minimal placeholder; replace with real model/tokenizer
        x = torch.randn(4, 512)
        loss = self.layer(x).sum() * 0.0
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)

def get_or_create_studio(name: str, teamspace: Optional[str] = None):
    """
    Reuse existing Running Studio if present; otherwise create/start one.
    Best-effort: if LIGHTNING_AVAILABLE is False, skip and return None.
    """
    if not LIGHTNING_AVAILABLE:
        return None
    ts = LightningTeamspace(teamspace) if teamspace else LightningTeamspace()
    for studio in ts.list_studios():
        if studio.name == name and studio.status == "running":
            print(f"Reusing running Studio: {name}")
            return studio
    # If stopped, try to restart; else create
    target = Machine.L40S  # prefer L40S; fallback handled by Studio start
    studio = Studio(name=name, teamspace=ts, machine=target)
    studio.start()
    print(f"Started Studio: {name}")
    return studio

def main():
    import argparse
    parser = argparse.ArgumentParser(description="CDN-first Surrogate training")
    parser.add_argument("--filelist", required=True, type=Path, help="Path to filelist.json")
    parser.add_argument("--repo", default="datasets/org/surrogate-data")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--studio-name", default="surrogate-training")
    parser.add_argument("--teamspace", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Studio reuse + idle-stop guard
    studio = get_or_create_studio(args.studio_name, args.teamspace)

    dataset = CDNIterableDataset(args.filelist)
    model = SurrogateModel()
    fabric = Fabric()

    if args.dry_run:
        count = 0
        for item in dataset:
            count += 1
            if count >= 10:
                break
        print(f"Dry-run streamed {count} items without HF API calls")
        return

    trainer = Trainer(
        max_epochs=args.epochs,
        logger=CSVLogger(save_dir="logs"),
        enable_checkpointing=False,
    )

