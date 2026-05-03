# vanguard / quality

## Final Synthesis (Correctness + Actionability)

### Diagnosis (resolved)
- **Runtime HF API calls** (`list_repo_tree`, `load_dataset`) burn quota and risk 429s.  
  **Fix:** Generate a static `file_manifest.json` once; training uses only CDN URLs.
- **Schema heterogeneity** in parquet files causes `pyarrow.CastError`.  
  **Fix:** Read with pyarrow, project to `{prompt,response}` at parse time, coerce to string.
- **No retry/backoff** for transient CDN/network failures.  
  **Fix:** Exponential backoff with jitter on fetch, per-file skip on permanent failure.
- **Lightning Studio reuse** missing; duplicate runs burn quota.  
  **Fix:** Detect running studio by name; reuse if running, else create.

### One Canonical Implementation

#### 1) Generate static manifest (single API call)

`/opt/axentx/vanguard/training/manifest.py`

```python
#!/usr/bin/env python3
"""
Generate a static CDN manifest for a date folder.
Usage:
  HF_TOKEN=... python manifest.py --repo org/repo --date 2026-04-29 --out manifest.json
"""
import argparse
import json
import os
import time
from pathlib import Path

from huggingface_hub import HfApi, login

HF_TOKEN = os.getenv("HF_TOKEN")
MAX_RETRIES = 5
BASE_BACKOFF = 3.0

def list_date_folder(api: HfApi, repo_id: str, date: str) -> list[str]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            items = api.list_repo_tree(repo_id=repo_id, path=date, recursive=False)
            paths = []
            for it in items:
                p = it.get("path") if isinstance(it, dict) else getattr(it, "path", None)
                if p:
                    paths.append(p)
            return paths
        except Exception as e:
            if "429" in str(e) and attempt < MAX_RETRIES:
                wait = BASE_BACKOFF * (2 ** (attempt - 1))
                print(f"Rate limited. Waiting {wait:.1f}s (attempt {attempt})")
                time.sleep(wait)
                continue
            raise

def build_manifest(repo_id: str, date: str, patterns=("parquet",)) -> list[dict]:
    api = HfApi(token=HF_TOKEN)
    if HF_TOKEN:
        login(token=HF_TOKEN, add_to_git_credential=False)
    paths = list_date_folder(api, repo_id, date)
    selected = [p for p in paths if any(p.endswith(f".{ext}") for ext in patterns)]
    base = f"https://huggingface.co/datasets/{repo_id}/resolve/main"
    manifest = [
        {"repo_id": repo_id, "path": p, "cdn_url": f"{base}/{p}"}
        for p in sorted(selected)
    ]
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN manifest for training")
    parser.add_argument("--repo", required=True, help="HF dataset repo (org/repo)")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-04-29)")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    args = parser.parse_args()

    manifest = build_manifest(args.repo, args.date)
    Path(args.out).write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest)} entries to {args.out}")

if __name__ == "__main__":
    main()
```

#### 2) Training loader (CDN-only, schema-safe, retry)

Key excerpts for `/opt/axentx/vanguard/training/train.py`

```python
#!/usr/bin/env python3
import json
import time
import random
from pathlib import Path
from typing import List

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

# ---- CDN fetch with exponential backoff + jitter ----
def robust_cdn_get(url: str, max_retries: int = 5, base_backoff: float = 1.0) -> bytes:
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            if attempt == max_retries:
                raise
            wait = base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"CDN fetch failed ({url}): {e}. Retry {attempt}/{max_retries} in {wait:.2f}s")
            time.sleep(wait)

# ---- Schema-safe parquet loader ----
def load_parquet_from_cdn(cdn_url: str) -> pd.DataFrame:
    data = robust_cdn_get(cdn_url)
    table = pq.read_table(pa.BufferReader(data))
    df = table.to_pandas()

    # Project to expected fields; tolerate missing/heterogeneous columns
    prompt_src = df.get("prompt", df.get("input", df.get("text", "")))
    response_src = df.get("response", df.get("output", ""))

    out = pd.DataFrame({
        "prompt": pd.Series(prompt_src).astype(str),
        "response": pd.Series(response_src).astype(str),
    })
    return out

# ---- Manifest-based dataset loader ----
def load_dataset_from_manifest(manifest_path: Path) -> pd.DataFrame:
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, list) or not all("cdn_url" in item for item in manifest):
        raise ValueError("Invalid manifest format")

    frames: List[pd.DataFrame] = []
    skipped = 0
    for item in manifest:
        try:
            df = load_parquet_from_cdn(item["cdn_url"])
            if not df.empty:
                frames.append(df)
        except Exception as e:
            skipped += 1
            print(f"Skipping {item['cdn_url']}: {e}")

    if not frames:
        raise RuntimeError("No data loaded from manifest")
    if skipped:
        print(f"Skipped {skipped}/{len(manifest)} files")
    return pd.concat(frames, ignore_index=True)

# ---- Lightning Studio reuse ----
def get_or_create_studio(name: str, machine_type, create_ok: bool = True):
    from lightning import Studio, Teamspace
    for s in Teamspace.studios:
        if getattr(s, "name", None) == name and getattr(s, "status", None) == "Running":
            print(f"Reusing running studio: {name}")
            return s
    if create_ok:
        print(f"Creating studio: {name}")
        return Studio(name=name, machine=machine_type, create_ok=True)
    raise RuntimeError(f"No running studio named {name}")

# ---- Training entry ----
def main_train():
    manifest_path = Path(__file__).parent / "file_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Generate manifest first: {manifest_path}")

    dataset = load_dataset_from_manifest(manifest_path)
    print(f"Loaded {len(dataset)} samples via CDN")
    # Continue training with Lightning/Fabric
```

#### 3) Orchestration (reuse + launch)

`scripts/launch_vanguard_training.py`

```python
#!/usr/bin/env python3
from lightning import Machine, Studio
from vanguard.training.train import get_or_create_studio

studio = get_or_create_studio("vanguard-quality", Machine.L40S, create_ok=True)
if getattr(studio, "status", None) != "Running":
    studio.start(machine=Machine.L40S)

job = studio.run(
    "python -m vanguard.training.train",
    name="vanguard-quality-run",
)
print(job)
```

#### 4) Verification (one-shot)

```bash
# Generate manifest once (single API call)
HF_TOKEN=... python /opt/axentx/vanguard/training/manifest.py \
  --repo org/repo --date 2026-04-29 --out /opt/axentx/vanguard/training/file_manifest.json

# Run training (CDN-only; reuses studio if running)
python /opt/axentx/vanguard/training/train
