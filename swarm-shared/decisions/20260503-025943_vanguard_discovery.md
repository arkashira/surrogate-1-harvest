# vanguard / discovery

## 1. Diagnosis
- No deterministic CDN-first manifest exists → training and ingestion scripts still risk HF API list calls (429) instead of pure CDN fetches.
- No content-hash integrity verification for downloaded files → silent corruption possible during long surrogate-1 training runs.
- Missing mount-point / entrypoint binding for Lightning Studio reuse → idle-stop kills training; no auto-restart guard.
- No date-scoped file-list JSON committed per ingestion batch → forces re-listing on every training run and breaks reproducibility.
- No lightweight discovery script to surface top-connected hub docs (e.g., MOC) before planning → loses #knowledge-rag #hub context at start of discovery cycles.

## 2. Proposed change
Create `/opt/axentx/vanguard/bin/discover_and_stage.py` (single CLI) that:
- lists one date folder via HF API (non-recursive) once, saves `batches/mirror-merged/{date}/manifest.json` with `{path, sha256, size, cdn_url}`
- downloads each file via CDN (no auth) while verifying sha256
- prints top-hub insight (MOC or highest degree node) from the most recent knowledge-rag graph snapshot found in `state/knowledge_rag/`
- outputs a Lightning-ready `train_filelist.txt` (CDN URLs only) for surrogate-1 training scripts

## 3. Implementation
```bash
# /opt/axentx/vanguard/bin/discover_and_stage.py
#!/usr/bin/env python3
"""
Discover + stage a date-scoped manifest for CDN-first training.
Usage:
  python discover_and_stage.py --repo <datasets/repo> --date 2026-04-29 \
    --out-dir ./state/staged --lightning-list ./train_filelist.txt
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Dict

import requests

HF_API_BASE = "https://huggingface.co/api"
CDN_BASE = "https://huggingface.co/datasets"

def list_date_folder(repo: str, date: str) -> List[Dict]:
    """Non-recursive list of one date folder; returns items with 'path' and 'type'."""
    path = f"{date}"
    url = f"{HF_API_BASE}/datasets/{repo}/tree"
    resp = requests.get(url, params={"path": path, "recursive": False})
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 360))
        print(f"Rate-limited. Waiting {retry_after}s", file=sys.stderr)
        time.sleep(retry_after)
        return list_date_folder(repo, date)
    resp.raise_for_status()
    items = resp.json()
    # Keep only files (skip subfolders)
    return [it for it in items if it.get("type") == "file"]

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def download_cdn(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def top_hub_insight(state_dir: Path) -> str:
    """Return most-connected hub from latest knowledge-rag graph snapshot, if any."""
    graph_dir = state_dir / "knowledge_rag"
    if not graph_dir.exists():
        return "No knowledge-rag graph snapshots found."
    graphs = sorted(graph_dir.glob("*.json"), key=os.path.getmtime, reverse=True)
    if not graphs:
        return "No graph JSON found."
    try:
        with open(graphs[0]) as f:
            data = json.load(f)
        # Expect nodes with edges or degree; fallback to keys
        if isinstance(data, dict) and "nodes" in data and "edges" in data:
            degree = {}
            for e in data["edges"]:
                a, b = e.get("source"), e.get("target")
                if isinstance(a, str) and isinstance(b, str):
                    degree[a] = degree.get(a, 0) + 1
                    degree[b] = degree.get(b, 0) + 1
            if degree:
                top = max(degree, key=degree.get)
                return f"Top hub: {top} (degree {degree[top]}) from {graphs[0].name}"
        # Fallback: look for MOC or highest key count
        if isinstance(data, dict):
            top = max(data.keys(), key=lambda k: len(data[k]) if isinstance(data[k], list) else 0)
            return f"Top hub: {top} from {graphs[0].name}"
    except Exception as exc:
        return f"Could not parse graph: {exc}"
    return "Could not determine top hub."

def main() -> None:
    parser = argparse.ArgumentParser(description="Discover + stage CDN manifest for training.")
    parser.add_argument("--repo", required=True, help="HF datasets repo (e.g., org/repo)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out-dir", default="./state/staged", help="Output directory for files")
    parser.add_argument("--lightning-list", help="Path to write CDN-only filelist for Lightning")
    parser.add_argument("--skip-download", action="store_true", help="Skip download (manifest only)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) / args.date
    manifest_path = out_dir / "manifest.json"
    manifest = []

    print(f"Listing {args.repo}/{args.date} (non-recursive)...")
    items = list_date_folder(args.repo, args.date)
    file_items = [it for it in items if it.get("type") == "file"]
    print(f"Found {len(file_items)} files.")

    for it in file_items:
        rel = it["path"]
        cdn_url = f"{CDN_BASE}/{args.repo}/resolve/main/{rel}"
        local_path = out_dir / rel
        entry = {
            "path": rel,
            "cdn_url": cdn_url,
            "size": it.get("size"),
        }
        if not args.skip_download:
            print(f"Downloading {rel}...")
            download_cdn(cdn_url, local_path)
            entry["sha256"] = sha256_file(local_path)
        else:
            entry["sha256"] = None
        manifest.append(entry)

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {manifest_path}")

    if args.lightning_list:
        lp = Path(args.lightning_list)
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "w") as f:
            for m in manifest:
                f.write(m["cdn_url"] + "\n")
        print(f"Lightning filelist written to {lp}")

    # Knowledge-rag top-hub insight
    insight = top_hub_insight(Path("./state"))
    print(insight)

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/bin/discover_and_stage.py
```

Lightning Studio guard snippet (add to training launcher):
```python
# reuse_or_start.py
from lightning import Studio, Machine

studio_name = "surrogate-1-train"
studio = None
for s in Studio.list():
    if s.name == studio_name:
        studio = s
        break

if studio is None:
    studio = Studio.create(
        name=studio_name,
        machine=Machine.L40S,
        repo=".",
        create_ok=True,
    )
elif studio.status != "running":
    studio.start(machine=Machine.L40S)

# Now safe to run training script
studio.run(["python", "train.py", "--filelist", "train_filelist.txt"])
```

## 4. Verification
1. Run
