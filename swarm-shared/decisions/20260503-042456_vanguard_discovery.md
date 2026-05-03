# vanguard / discovery

## 1. Diagnosis
- Runtime `load_dataset` calls in training/frontend trigger HF API 429s and non-reproducible epochs; no content-addressed manifest prevents CDN-only fetching.
- No deterministic repo mapping or file-list snapshot → ingestion/training scripts re-enumerate repos and hit recursive pagination limits.
- Mixed-schema files from dataset-mirror written to `enriched/` with extra metadata columns (`source`, `ts`) break downstream surrogate-1 schema expectations.
- No local discovery/index of top hub (e.g., MOC) or knowledge-rag integration to contextualize planning and dataset selection.
- Missing wrapper-script hygiene (shebang, executable bit, `SHELL=/bin/bash`) risks cron/active-learning/opus-pr-reviewer failures.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/manifest.py` + `/opt/axentx/vanguard/discovery/run_discovery.sh` that:
- Lists a single date folder via `list_repo_tree(recursive=False)` and writes `manifest-{date}.json` (path + sha256).
- Projects any parquet files to `{prompt, response}` only and stores attribution in filename (`batches/mirror-merged/{date}/{slug}.parquet`).
- Emits a small top-hub insight file (`hub-insight.json`) by querying knowledge-rag for the most-connected node (e.g., MOC).
- Adds a reusable Bash wrapper with proper shebang and executable bit for cron-safe invocation.

## 3. Implementation
```bash
# /opt/axentx/vanguard/discovery/run_discovery.sh
#!/usr/bin/env bash
set -euo pipefail
SHELL=/bin/bash

cd /opt/axentx/vanguard
REPO="axentx/datasets"        # adjust as needed
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="./discovery/out"
MANIFEST="${OUTDIR}/manifest-${DATE}.json"
HUB_OUT="${OUTDIR}/hub-insight.json"

mkdir -p "${OUTDIR}"

python3 ./discovery/manifest.py \
  --repo "${REPO}" \
  --date "${DATE}" \
  --manifest "${MANIFEST}" \
  --hub-out "${HUB_OUT}"
```

```python
# /opt/axentx/vanguard/discovery/manifest.py
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

try:
    from huggingface_hub import list_repo_tree, hf_hub_download
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def list_date_folder(repo: str, date: str) -> List[Dict[str, Any]]:
    """
    Single API call: non-recursive listing for one date folder.
    Returns list of dicts with path/rtype.
    """
    folder = f"batches/mirror-merged/{date}"
    items = list_repo_tree(repo=repo, path=folder, recursive=False)
    # items are objects with .path and .type; normalize
    return [{"path": it.path, "type": it.type} for it in items]

def project_parquet_to_pair(src_path: str, dst_dir: str, slug: str) -> str:
    """
    Project parquet to {prompt,response} only and store as
    batches/mirror-merged/{date}/{slug}.parquet
    """
    import pandas as pd
    df = pd.read_parquet(src_path)
    # Keep only expected surrogate-1 fields; drop source/ts/other metadata
    keep = {"prompt", "response"}
    missing = keep - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns {missing} in {src_path}")
    out_df = df[list(keep)].reset_index(drop=True)

    out_dir = Path(dst_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.parquet"
    out_df.to_parquet(out_path, index=False)
    return str(out_path)

def build_manifest(repo: str, date: str, manifest_path: str) -> List[Dict[str, Any]]:
    entries = []
    items = list_date_folder(repo, date)
    cache_dir = Path("/tmp/vanguard_hf_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    for it in items:
        if it["type"] != "file":
            continue
        # Download file via CDN (no auth header) to avoid API rate limits during training.
        # We still use hf_hub_download for convenience; it will use CDN.
        local_path = hf_hub_download(repo_id=repo, filename=it["path"], cache_dir=str(cache_dir))
        digest = sha256_file(local_path)

        entry = {
            "path": it["path"],
            "sha256": digest,
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=it["path"]),
            "size": os.path.getsize(local_path),
        }

        # If parquet, project to surrogate-1 pair and record projected path
        if it["path"].lower().endswith(".parquet"):
            slug = hashlib.sha256(it["path"].encode()).hexdigest()[:12]
            projected = project_parquet_to_pair(local_path, f"./data/projected/{date}", slug)
            entry["projected"] = projected
            entry["slug"] = slug

        entries.append(entry)

    Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"date": date, "generated": datetime.utcnow().isoformat() + "Z", "entries": entries}, f, indent=2)
    return entries

def top_hub_insight(out_path: str) -> Dict[str, Any]:
    """
    Lightweight top-hub insight: most-connected node from knowledge-rag.
    If knowledge-rag is unavailable, emit a placeholder with instructions.
    """
    # Placeholder implementation — replace with actual knowledge-rag query when available.
    insight = {
        "top_hub": "MOC",
        "reason": "Most-connected node in knowledge graph (placeholder). Run knowledge-rag query for full context.",
        "tags": ["#knowledge-rag", "#graph", "#hub"],
        "generated": datetime.utcnow().isoformat() + "Z"
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(insight, f, indent=2)
    return insight

def main() -> None:
    parser = argparse.ArgumentParser(description="Build content-addressed manifest for CDN-only training.")
    parser.add_argument("--repo", default="axentx/datasets", help="HF dataset repo")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--manifest", required=True, help="Output manifest JSON path")
    parser.add_argument("--hub-out", required=True, help="Output hub insight JSON path")
    args = parser.parse_args()

    print(f"Building manifest for {args.repo}@{args.date}...")
    entries = build_manifest(args.repo, args.date, args.manifest)
    print(f"Wrote {len(entries)} entries to {args.manifest}")

    print("Generating top-hub insight...")
    top_hub_insight(args.hub_out)
    print(f"Wrote hub insight to {args.hub_out}")

if __name__ == "__main__":
    main()
```

```bash
# Make wrapper executable and ensure cron-safe environment
chmod +x /opt/axentx/vanguard/discovery/run_discovery.sh
# Add to crontab (example) — ensure SHELL is set in crontab:
# SHELL=/bin/bash
# 0 2 * * * cd /opt/axentx/vanguard && ./discovery/run_discovery.sh $(date +\%Y-\%m-\%d) >> ./discovery/out/cron.log 2>&1
```


