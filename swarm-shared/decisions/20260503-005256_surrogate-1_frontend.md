# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value change**: Add a Mac-side `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs, and a training loader that uses **CDN-only** fetches (zero HF API calls during training). This implements the CDN bypass pattern and eliminates 429 rate-limit failures during data loading in Lightning Studio.

### Steps (1h 30m total)

1. **Create tools/snapshot_manifest.py** (20m)  
   - Single API call: `list_repo_tree(path=date_partition, recursive=True)`  
   - Filter to `.jsonl`/`.parquet` files  
   - Emit deterministic `file_manifest.json` with CDN URLs and sizes  
   - Include `generated_at` and `partition` metadata

2. **Create training/data_loader.py** (30m)  
   - Load `file_manifest.json`  
   - Use `datasets.load_dataset` with `data_files` pointing to CDN URLs (zero HF API calls)  
   - Parse JSONL/Parquet into Arrow format  
   - Yield `{prompt, response}` pairs

3. **Update training script** (20m)  
   - Replace `load_dataset(streaming=True)` with CDN loader  
   - Add CLI arg `--manifest file_manifest.json`  
   - Keep fallback to HF API only if manifest missing (for dev)

4. **Add to README** (10m)  
   - Usage: `python tools/snapshot_manifest.py --date 2026-05-03 --out manifest.json`  
   - Note: Manifest generation requires HF token; training does not

5. **Test locally** (10m)  
   - Generate manifest for a small date partition  
   - Run training loader, verify records parsed

---

## Code Snippets

### tools/snapshot_manifest.py
```python
#!/usr/bin/env python3
"""
Generate CDN-only file manifest for a date partition in surrogate-1-training-pairs.
Usage:
  HF_TOKEN=hf_xxx python tools/snapshot_manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --partition 2026-04-29 \
    --out manifests/2026-04-29.manifest.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import List, Dict

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def build_manifest(repo: str, partition: str) -> Dict:
    """Single HF API call to list files under a date partition."""
    api = HfApi(token=os.getenv("HF_TOKEN"))
    prefix = f"batches/public-merged/{partition}"
    entries = api.list_repo_tree(repo=repo, path=prefix, recursive=True, repo_type="dataset")

    files = []
    for e in sorted(entries, key=lambda x: x.path):
        if e.type != "file":
            continue
        if not (e.path.endswith(".jsonl") or e.path.endswith(".parquet")):
            continue
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=e.path)
        files.append(
            {
                "path": e.path,
                "cdn_url": cdn_url,
                "size": getattr(e, "size", None),
            }
        )

    manifest = {
        "repo": repo,
        "partition": partition,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Generate CDN manifest for a partition.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--partition", required=True, help="Date partition, e.g. 2026-04-29")
    parser.add_argument("--out", default=None, help="Output JSON path (default: stdout)")
    args = parser.parse_args()

    manifest = build_manifest(args.repo, args.partition)
    out = sys.stdout if args.out is None else open(args.out, "w", encoding="utf-8")
    json.dump(manifest, out, indent=2)
    if out is not sys.stdout:
        out.close()
        print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
```

### training/data_loader.py
```python
import json
from pathlib import Path

from datasets import load_dataset


def load_partition_from_manifest(manifest_path: str):
    """Load partition using CDN URLs from manifest (zero HF API calls)."""
    with open(manifest_path) as f:
        manifest = json.load(f)

    cdn_urls = [f["cdn_url"] for f in manifest["files"]]
    if not cdn_urls:
        raise ValueError("No files in manifest")

    # CDN URLs are public; no HF API calls during data loading.
    ds = load_dataset("json", data_files=cdn_urls, split="train")
    return ds
```

### training/train.py (minimal example)
```python
import argparse
from training.data_loader import load_partition_from_manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to file_manifest.json")
    args = parser.parse_args()

    ds = load_partition_from_manifest(args.manifest)
    print(f"Loaded {len(ds)} records from CDN")
    # Continue with training...


if __name__ == "__main__":
    main()
```

### README addition (snippet)
```markdown
## Generate CDN manifest (Mac/Linux)

```bash
# One-time API call per partition (after rate-limit window clears)
HF_TOKEN=hf_xxx python tools/snapshot_manifest.py \
  --repo axentx/surrogate-1-training-pairs \
  --partition 2026-04-29 \
  --out manifests/2026-04-29.manifest.json
```

Then train with CDN-only fetches (zero HF API calls during training):

```python
from training.data_loader import load_partition_from_manifest
ds = load_partition_from_manifest("manifests/2026-04-29.manifest.json")
```
```

---

## Notes & trade-offs
- **Speed**: Single API call per partition; training uses CDN bandwidth only.
- **Safety**: Manifest pins exact file set; avoids schema surprises by using same paths the runners produce.
- **No state leakage**: Manifest is immutable per partition; runners can continue uploading new shards without invalidating existing manifests.
- **Mac rule respected**: Mac only runs orchestration (snapshot script) and Lightning SDK launcher; heavy training happens on Lightning Studio using the CDN manifest.
