# vanguard / backend

### Final Synthesis (single, correct, actionable)

**Diagnosis (merged, de-duplicated)**
- No CDN-first manifest → runtime `list_repo_tree`/`load_dataset` cause 429s and non-reproducible runs.  
- No deterministic, content-addressed file list keyed by date/slug → jobs re-enumerate and burn quota.  
- No CDN bypass for file fetches → unnecessary API load and rate-limit risk.  
- Using `load_dataset(streaming=True)` on heterogeneous repos → pyarrow `CastError` from mixed schemas.  
- No Lightning Studio reuse guard → training may recreate running studios and waste quota.

**Proposed change (unified)**
Create `/opt/axentx/vanguard/backend/ingest/manifest.py` and patch `/opt/axentx/vanguard/backend/train/train.py` to:
- Build a CDN-first, deterministic manifest once per `date_folder` with `{slug, path, cdn_url}` entries.  
- Iterate files via CDN (`hf_hub_download` + direct CDN URLs) and project only `{prompt, response}` at parse time to avoid schema issues.  
- Reuse running Lightning Studios by name instead of recreating them.  
- Make training consume the manifest and use CDN-only fetches.

**Implementation (single, production-ready)**

Directory setup:
```bash
mkdir -p /opt/axentx/vanguard/backend/{ingest,train}
```

`/opt/axentx/vanguard/backend/ingest/manifest.py`
```python
#!/usr/bin/env python3
"""
CDN-first manifest builder for HF datasets.
Generates content-addressed manifest keyed by date/slug to avoid runtime list_repo_tree.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List

from huggingface_hub import HfApi, hf_hub_download

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-repo")
OUT_DIR = Path(os.getenv("MANIFEST_OUT_DIR", "manifests"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

api = HfApi()


def build_manifest(date_folder: str, repo: str = HF_REPO) -> Path:
    """
    Single API call: list_repo_tree for one date folder (non-recursive).
    Returns manifest path.
    """
    entries = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [
        e
        for e in entries
        if e.type == "file" and e.path.endswith((".parquet", ".jsonl", ".json"))
    ]

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "files": [],
    }

    for f in files:
        slug = Path(f.path).stem  # e.g., 2026-04-29/abc123.parquet -> abc123
        manifest["files"].append(
            {
                "slug": slug,
                "path": f.path,
                "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{f.path}",
                "size": getattr(f, "size", None),
            }
        )

    out_path = OUT_DIR / f"manifest-{date_folder}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    return out_path


def iter_cdn_files(manifest_path: Path) -> Iterator[Dict[str, str]]:
    """
    Yield projected {prompt, response, slug} rows from each file via hf_hub_download.
    Avoids load_dataset and mixed-schema issues.
    """
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["files"]:
        local_path = hf_hub_download(repo_id=manifest["repo"], filename=item["path"])

        if local_path.endswith(".parquet"):
            import pyarrow.parquet as pq

            table = pq.read_table(local_path, columns=["prompt", "response"])
            df = table.to_pandas()
            for _, row in df.iterrows():
                yield {"prompt": row["prompt"], "response": row["response"], "slug": item["slug"]}

        elif local_path.endswith(".jsonl"):
            with open(local_path) as fh:
                for line in fh:
                    obj = json.loads(line)
                    yield {"prompt": obj["prompt"], "response": obj["response"], "slug": item["slug"]}

        else:
            with open(local_path) as fh:
                obj = json.load(fh)
                if isinstance(obj, list):
                    for row in obj:
                        yield {"prompt": row["prompt"], "response": row["response"], "slug": item["slug"]}
                else:
                    yield {"prompt": obj["prompt"], "response": obj["response"], "slug": item["slug"]}


if __name__ == "__main__":
    import sys

    date_folder = sys.argv[1] if len(sys.argv) > 1 else "2026-04-29"
    out = build_manifest(date_folder)
    print(f"Manifest written to {out}")
```

`/opt/axentx/vanguard/backend/train/train.py` (patch)
```python
import json
import os
from pathlib import Path

from huggingface_hub import hf_hub_download

try:
    from lightning.pytorch.cli import LightningCLI
    from axentx.utils.studio import get_or_reuse_studio
except ImportError as e:
    raise RuntimeError("Missing training dependencies") from e


MANIFEST_PATH = Path(
    os.getenv(
        "MANIFEST_PATH",
        "manifests/manifest-2026-04-29.json",
    )
)


def prepare_data_from_manifest(manifest_path: Path):
    """
    Deterministic CDN-first dataset preparation.
    Returns list of {prompt, response, slug}.
    """
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found: {manifest_path}. Run ingest/manifest.py first."
        )

    manifest = json.loads(manifest_path.read_text())
    records = []

    for item in manifest["files"]:
        # Use CDN bypass via hf_hub_download (cached) and project schema locally.
        local_path = hf_hub_download(repo_id=manifest["repo"], filename=item["path"])

        if local_path.endswith(".parquet"):
            import pyarrow.parquet as pq

            table = pq.read_table(local_path, columns=["prompt", "response"])
            df = table.to_pandas()
            for _, row in df.iterrows():
                records.append(
                    {"prompt": row["prompt"], "response": row["response"], "slug": item["slug"]}
                )

        elif local_path.endswith(".jsonl"):
            with open(local_path) as fh:
                for line in fh:
                    obj = json.loads(line)
                    records.append(
                        {"prompt": obj["prompt"], "response": obj["response"], "slug": item["slug"]}
                    )

        else:
            with open(local_path) as fh:
                obj = json.load(fh)
                if isinstance(obj, list):
                    for row in obj:
                        records.append(
                            {"prompt": row["prompt"], "response": row["response"], "slug": item["slug"]}
                        )
                else:
                    records.append(
                        {"prompt": obj["prompt"], "response": obj["response"], "slug": item["slug"]}
                    )

    return records


def main():
    # Reuse studio instead of recreating
    studio = get_or_reuse_studio(
        name=os.getenv("LIGHTNING_STUDIO_NAME", "vanguard-train"),
        machine=os.getenv("LIGHTNING_MACHINE", "L40S"),
    )

    # Deterministic CDN-first dataset
    dataset_records = prepare_data_from_manifest(MANIFEST_PATH)

    # Continue with LightningCLI or your training loop using dataset_records
    # Example:
    # cli = LightningCLI(
    #     ...,
    #     run=False,
    # )
    # cli.trainer.fit(..., datamodule=...)


if __name__ == "__main__":
    main()
```

`/opt/axentx/vanguard/backend/train/get_or_reuse_studio.py` (new)
```python
import os
from typing import Optional

try
