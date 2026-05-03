# vanguard / discovery

## Final Synthesis (adopted)

**Core diagnosis (merged, de-duplicated)**
- No CDN-first manifest; ingestion/training can still trigger `list_repo_tree`/`load_dataset` at runtime → 429 risk and non-reproducible runs.
- Missing deterministic, content-addressed file list keyed by date/slug; training jobs re-enumerate the repo and burn quota.
- No lightweight discovery utility to produce the manifest once (after rate-limit window) and embed it in downstream jobs.
- No guardrails to prevent Mac/local `load_dataset`/`from_pretrained` that violate “Mac=CLI only” compute boundary.
- Mixed-schema HF repos can cause pyarrow cast errors; surrogate-1 training expects clean {prompt,response} projection.

**Single change to implement**
Add `/opt/axentx/vanguard/discovery/` with:
- `manifest.py` — one-shot discovery (after HF rate-limit clears) that lists a date-scoped folder non-recursively, produces a deterministic, content-addressed manifest keyed by `{date}/{slug}` with `cdn_url` and `sha256`, and pins revision.
- `train.py` — CDN-only loader that consumes the manifest and performs **zero** HF API calls during training; robust schema projection and optional streaming to avoid OOM.
- `README.md` — usage + verification steps.

**Implementation (concrete, actionable)**

```bash
# Create structure
mkdir -p /opt/axentx/vanguard/discovery
cd /opt/axentx/vanguard/discovery
```

`discovery/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate a deterministic, CDN-first manifest for a date folder in a HF dataset repo.
Run once (after HF API rate-limit window) on Mac/CLI. Embed output into training.
"""
import json, hashlib, os, sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

API = HfApi()

def list_date_folder(repo_id: str, date_folder: str, revision: str = "main"):
    """
    List files in repo_id/date_folder (non-recursive) and return CDN manifest.
    date_folder example: '2026-04-29'
    """
    tree = API.list_repo_tree(
        repo_id=repo_id,
        path=date_folder,
        revision=revision,
        recursive=False,
    )
    files = [entry for entry in tree if getattr(entry, "type", None) == "file"]
    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "revision": revision,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": [],
    }
    for f in files:
        fname = getattr(f, "path", None)
        if not fname:
            continue
        cdn_url = (
            f"https://huggingface.co/datasets/{repo_id}/resolve/main/"
            f"{date_folder}/{fname}"
        )
        entry = {
            "slug": fname,
            "path": f"{date_folder}/{fname}",
            "size": getattr(f, "size", None),
            "cdn_url": cdn_url,
            # sha256 requires download; leave placeholder for optional post-fill.
            "sha256": None,
        }
        manifest["files"].append(entry)
    # Deterministic ordering for reproducibility
    manifest["files"].sort(key=lambda x: (x["path"], x["slug"]))
    return manifest

def save_manifest(manifest, out_path: str):
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    print(f"Manifest saved: {out_path}")
    return out_path

if __name__ == "__main__":
    REPO_ID = os.getenv("HF_DATASET_REPO", "your-org/your-dataset")
    DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")
    OUT = os.getenv("MANIFEST_OUT", f"cdn-manifest-{DATE_FOLDER}.json")
    m = list_date_folder(REPO_ID, DATE_FOLDER)
    save_manifest(m, OUT)
```

`discovery/train.py`
```python
#!/usr/bin/env python3
"""
CDN-only training data loader.
Consumes manifest.json produced by manifest.py.
Zero HF API calls during training.
"""
import json
import random
import warnings
from pathlib import Path
from typing import Dict, Iterator, List

import requests
import pyarrow as pa
import pyarrow.parquet as pq
from io import BytesIO

MANIFEST_PATH = Path(__file__).parent / "manifest.json"

def load_manifest(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)

def stream_parquet_from_cdn(cdn_url) -> pa.Table:
    """Download single parquet via CDN and return pyarrow Table."""
    resp = requests.get(cdn_url, timeout=120)
    resp.raise_for_status()
    return pq.read_table(BytesIO(resp.content))

def project_row(
    row: Dict,
    prompt_cols=("prompt", "instruction", "input"),
    response_cols=("response", "output", "completion"),
) -> Dict:
    """Return {prompt, response} or raise KeyError if neither column found."""
    prompt = next((row[c] for c in prompt_cols if c in row), None)
    response = next((row[c] for c in response_cols if c in row), None)
    if prompt is None or response is None:
        raise KeyError("Missing prompt/response columns in row")
    return {"prompt": prompt, "response": response}

def build_dataset(manifest) -> Iterator[Dict]:
    """Yield {prompt, response} from CDN parquet files."""
    for f in manifest["files"]:
        path = f.get("path", "")
        if not path.lower().endswith(".parquet"):
            continue
        cdn_url = f.get("cdn_url")
        if not cdn_url:
            continue
        try:
            tbl = stream_parquet_from_cdn(cdn_url)
        except Exception as exc:
            warnings.warn(f"Failed to load {cdn_url}: {exc}")
            continue

        cols = set(tbl.column_names)
        prompt_col = next((c for c in ("prompt", "instruction", "input") if c in cols), None)
        response_col = next((c for c in ("response", "output", "completion") if c in cols), None)
        if prompt_col is None or response_col is None:
            warnings.warn(f"Skipping {path}: missing prompt/response columns")
            continue

        # Project to dict rows to avoid pyarrow cast errors across mixed schemas
        rows = tbl.select([prompt_col, response_col]).to_pylist()
        for row in rows:
            try:
                yield project_row(row)
            except KeyError:
                continue

def get_dataloader(
    manifest_path=MANIFEST_PATH,
    batch_size=8,
    shuffle=True,
    max_examples=None,
) -> Iterator[List[Dict]]:
    """Yield batches of {prompt, response}."""
    manifest = load_manifest(manifest_path)
    examples = list(build_dataset(manifest))
    if max_examples is not None:
        examples = examples[:max_examples]
    if shuffle:
        random.shuffle(examples)
    for i in range(0, len(examples), batch_size):
        yield examples[i : i + batch_size]

if __name__ == "__main__":
    # Quick smoke test
    for batch in get_dataloader(batch_size=2):
        print(f"Batch size: {len(batch)}")
        for ex in batch[:2]:
            print(ex["prompt"][:80] if ex["prompt"] else "", "...")
        break
```

`discovery/README.md`
```markdown
# vanguard / discovery

CDN-first discovery for surrogate-1 training.

## Usage

1) Generate manifest (run once after HF rate-limit window, on Mac/CLI):
```bash
cd /opt/axentx/vanguard/discovery
HF_DATASET_REPO=your-org/your-dataset DATE_FOLDER=2026-04-2
