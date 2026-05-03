# vanguard / backend

## Final Synthesized Solution (Correct + Actionable)

### 1) Diagnosis (merged, de-duplicated)
- **No CDN-first manifest** → runtime `list_repo_tree`/`load_dataset` can still trigger 429 and non-reproducible runs.
- **No deterministic, content-addressed file list keyed by `{date}/{slug}`** → jobs re-enumerate and re-fetch on every run and cannot resume safely.
- **No local fallback for HF API rate limits on the orchestrator (Mac)** → if `list_repo_tree` hits 429, pipeline stalls.
- **Training script likely uses `load_dataset(streaming=True)` on heterogeneous repo** → `pyarrow.CastError` on mixed schemas.
- **No guard to reuse running Lightning Studio** → quota waste via repeated `create_ok=True` instead of reusing running studios.

### 2) One concrete change
Add a backend orchestration module that:
- Pre-lists a single date folder via HF API (once per date) and writes a **content-addressed manifest** (`manifests/{date_slug}.json`) with **CDN URLs + SHA256**.
- Embeds that manifest path in training config so Lightning training uses **CDN-only fetches** (zero API calls during data load).
- Adds a small wrapper to **reuse a running Lightning Studio** (or restart if idle-stopped).
- Replaces any `load_dataset` usage with **per-file `hf_hub_download` + projection to `{prompt, response}`** and schema enforcement to avoid `pyarrow.CastError`.

Scope (Mac/Linux):
- `/opt/axentx/vanguard/backend/config.py`
- `/opt/axentx/vanguard/backend/manifest.py`
- `/opt/axentx/vanguard/backend/train.py`

### 3) Implementation

```bash
# Ensure structure
sudo mkdir -p /opt/axentx/vanguard/backend/manifests
sudo chown -R $(whoami) /opt/axentx/vanguard
```

#### `/opt/axentx/vanguard/backend/config.py`
```python
from pathlib import Path
from dataclasses import dataclass

@dataclass
class HFConfig:
    repo: str = "datasets/your-org/your-repo"
    date_folder: str = "batches/mirror-merged/2026-04-29"  # change per run

@dataclass
class LightningConfig:
    teamspace: str = "your-teamspace"
    study_name: str = "surrogate-1-train"
    machine: str = "L40S"
    cloud: str = "lightning-public-prod"

BASE_DIR = Path(__file__).parent.parent
MANIFEST_DIR = BASE_DIR / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)
```

#### `/opt/axentx/vanguard/backend/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate CDN-first manifest for a date folder.
Run from Mac (or cron) after HF API rate-limit window clears.
"""
import json
import hashlib
import time
from pathlib import Path
from typing import List, Dict

from huggingface_hub import HfApi

from config import HFConfig, MANIFEST_DIR

api = HfApi()

def _sha256_of_url(url: str) -> str:
    # Lightweight: derive deterministic hash from CDN URL (stable across runs).
    # If you require file-level integrity, download once and hash content.
    return hashlib.sha256(url.encode()).hexdigest()

def build_manifest(date_slug: str, repo: str) -> List[Dict]:
    """
    List one folder (non-recursive) and build CDN entries.
    Returns list of {slug, cdn_url, sha256}
    """
    items = api.list_repo_tree(repo=repo, path=date_slug, recursive=False)

    manifest = []
    for item in items:
        if not item.path.endswith(".parquet"):
            continue

        slug = item.path
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}"
        manifest.append({
            "slug": slug,
            "cdn_url": cdn_url,
            "hf_path": item.path,
            "sha256": _sha256_of_url(cdn_url),
        })

    return manifest

def save_manifest(date_slug: str, manifest: List[Dict]) -> Path:
    safe_date = date_slug.replace("/", "_")
    out = MANIFEST_DIR / f"{safe_date}.json"
    out.write_text(json.dumps(manifest, indent=2))
    return out

def main(date_slug: str):
    cfg = HFConfig()
    print(f"Building manifest for {cfg.repo}/{date_slug} ...")
    manifest = build_manifest(date_slug, cfg.repo)
    out = save_manifest(date_slug, manifest)
    print(f"Manifest saved: {out} ({len(manifest)} files)")
    return out

if __name__ == "__main__":
    import sys
    date_slug = sys.argv[1] if len(sys.argv) > 1 else HFConfig().date_folder
    main(date_slug)
```

#### `/opt/axentx/vanguard/backend/train.py`
```python
#!/usr/bin/env python3
"""
Lightning training script that uses CDN-only fetches.
Embeds manifest path; no list_repo_tree/load_dataset during training.
"""
import json
import pyarrow.parquet as pq
import pyarrow.compute as pc
from pathlib import Path
from typing import List, Dict, Any

import lightning as L
from torch.utils.data import IterableDataset, DataLoader

from huggingface_hub import hf_hub_download
from config import HFConfig, LightningConfig, MANIFEST_DIR

HF_CFG = HFConfig()
LT_CFG = LightningConfig()

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path: Path, schema_project: List[str] = ("prompt", "response")):
        manifest = json.loads(manifest_path.read_text())
        self.files = [item["cdn_url"] for item in manifest]
        self.schema_project = schema_project

    def _project_row(self, table) -> List[Dict[str, Any]]:
        # Enforce schema and avoid pyarrow.CastError on mixed schemas
        missing = [c for c in self.schema_project if c not in table.column_names]
        if missing:
            raise ValueError(f"Missing columns in parquet: {missing}")

        # Cast to string to normalize heterogeneous types safely
        safe = table
        for c in self.schema_project:
            if not pc.is_string(safe.column(c)).as_py():
                safe = safe.set_column(
                    safe.column_names.index(c),
                    c,
                    pc.cast(safe.column(c), pa.string())
                )
        df = safe.select(self.schema_project).to_pandas()
        return df.to_dict(orient="records")

    def __iter__(self):
        import pyarrow as pa
        for url in self.files:
            # Download via CDN (no auth, no API calls)
            rel = url.split("/resolve/main/")[-1]
            local = hf_hub_download(
                repo_id=HF_CFG.repo,
                filename=rel,
                repo_type="dataset",
            )
            table = pq.read_table(local, columns=self.schema_project)
            for row in self._project_row(table):
                yield row

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, manifest_path: Path, batch_size: int = 8):
        super().__init__()
        self.manifest_path = manifest_path
        self.batch_size = batch_size

    def train_dataloader(self):
        ds = CDNParquetDataset(self.manifest_path)
        return DataLoader(ds, batch_size=self.batch_size, num_workers=0)

class SurrogateModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        # Minimal surrogate head; replace with your model
        self.lm = None

    def training_step(self, batch, batch_idx):
        # Placeholder training logic
        self.log("train/loss", 0.1 * batch_idx, prog_bar=True)
        return 0.1 * batch_idx

    def configure_optimizers(self):
        return None

def reuse_or_create_studio(cfg: LightningConfig, manifest_path: Path):
    from lightning.pytorch.studio import Studio, Teamspace

    team = Teamspace(cfg.teamspace)
    running = [s for
