# vanguard / discovery

## Final consolidated solution

**Core diagnosis (merged)**
- No persisted `(repo, dateFolder)` manifest → every training run re-enumerates via authenticated HF API → quota burn + 429 risk.
- Training likely uses recursive `list_repo_tree`/`load_dataset` during data loading instead of CDN-only fetches.
- No local discovery utility to inspect date-folders/files before training.
- No Lightning Studio reuse → wastes quota (≈80 hr/mo).
- No idle-stop guard → idle timeout kills training; no auto-restart.

**Chosen approach**
- Keep discovery/manifest generation as a **separate, reusable CLI** (not embedded in training) so it can be run once per `(repo, dateFolder)` after a rate-limit window.
- Training script consumes a **CDN-only manifest** and never calls authenticated HF APIs during data loading.
- Add lightweight **Studio reuse + idle-restart** helper to launcher.
- Project heterogeneous files to `{prompt, response}` at parse time to avoid schema errors.
- Target `Lightning.L40S` via `lightning-lambda-prod` (H200 unavailable on free tier).

---

### 1. Discovery + manifest generator  
File: `/opt/axentx/vanguard/scripts/discover_and_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate a CDN-only file manifest for a Hugging Face dataset repo + dateFolder.
Usage:
    python discover_and_manifest.py \
        --repo <org/dataset> \
        --date-folder 2026-04-29 \
        --out-dir ./manifests
"""
import argparse
import json
import sys
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

HF_CDN_BASE = "https://huggingface.co/datasets"


def build_manifest(repo: str, date_folder: str, out_dir: str):
    api = HfApi()
    prefix = f"{date_folder}/"
    # Single non-recursive call per dateFolder to minimize API usage
    tree = api.list_repo_tree(repo=repo, path=prefix, recursive=False, repo_type="dataset")

    files = []
    for entry in tree:
        if entry.type != "file":
            continue
        # CDN path (no auth, bypasses API rate limits during training)
        cdn_url = f"{HF_CDN_BASE}/{repo}/resolve/main/{entry.path}"
        files.append({
            "repo": repo,
            "path": entry.path,
            "cdn_url": cdn_url,
            "size": getattr(entry, "size", None),
            "lfs": getattr(entry, "lfs", None)
        })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_by": "discover_and_manifest.py",
        "count": len(files),
        "files": files
    }

    out_path = Path(out_dir) / repo.replace("/", "_") / f"{date_folder}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path} ({len(files)} files)")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Generate CDN manifest for HF dataset dateFolder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo, e.g. org/dataset")
    parser.add_argument("--date-folder", required=True, help="Date folder inside dataset, e.g. 2026-04-29")
    parser.add_argument("--out-dir", default="./manifests", help="Output directory for manifests")
    args = parser.parse_args()

    try:
        build_manifest(args.repo, args.date_folder, args.out_dir)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/discover_and_manifest.py
```

---

### 2. Training launcher with manifest + Studio reuse + idle-restart  
File: `/opt/axentx/vanguard/train.py`

```python
#!/usr/bin/env python3
"""
Vanguard surrogate-1 training (discovery-focused).
- Uses CDN-only manifest (no HF API calls during data loading).
- Reuses running Lightning Studio; restarts if idle-stopped.
- Projects heterogeneous files to {prompt, response} at parse time.
- Target: Lightning.L40S via lightning-lambda-prod cloud account.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from lightning import Lightning, Teamspace, Machine, Cloud
    _LIGHTNING_AVAILABLE = True
except Exception:
    _LIGHTNING_AVAILABLE = False
    Lightning = Teamspace = Machine = Cloud = None  # dry-run friendly

HF_CDN_BASE = "https://huggingface.co/datasets"


# ---- Manifest helpers ----
def load_manifest(manifest_path: str):
    with open(manifest_path) as f:
        return json.load(f)


def cdn_url_to_local(manifest, base_cache_dir: str):
    """Map CDN URLs into a local cache directory layout."""
    cache_root = Path(base_cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for item in manifest["files"]:
        url = item["cdn_url"]
        rel = Path(item["path"])
        local_path = cache_root / manifest["repo"].replace("/", "_") / manifest["date_folder"] / rel
        local_path.parent.mkdir(parents=True, exist_ok=True)
        mapping[url] = local_path
    return mapping


# ---- Data parsing (project heterogeneous files to {prompt, response}) ----
def parse_record(raw_bytes, file_path: Path):
    """
    Lightweight parser that projects heterogeneous files into {prompt, response}.
    Extend per file type as needed.
    """
    name = file_path.name.lower()
    text = raw_bytes.decode("utf-8", errors="replace").strip()

    # JSONL: expect {"prompt": ..., "response": ...} or similar
    if name.endswith(".jsonl"):
        import json as _json
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        records = []
        for ln in lines:
            obj = _json.loads(ln)
            records.append({
                "prompt": obj.get("prompt") or obj.get("input") or "",
                "response": obj.get("response") or obj.get("output") or ""
            })
        return records

    # Plain text: treat first non-empty paragraph as prompt, remainder as response
    if name.endswith(".txt"):
        blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
        if len(blocks) >= 2:
            return [{"prompt": blocks[0], "response": "\n\n".join(blocks[1:])}]
        return [{"prompt": blocks[0] if blocks else "", "response": ""}]

    # CSV/TSV fallback: require columns prompt,response
    if name.endswith((".csv", ".tsv")):
        import csv
        rows = list(csv.DictReader(text.splitlines()))
        out = []
        for r in rows:
            out.append({
                "prompt": r.get("prompt", ""),
                "response": r.get("response", "")
            })
        return out

    # Default: single record with full text as prompt
    return [{"prompt": text, "response": ""}]


# ---- Lightning Studio helpers ----
def find_running_studio(name: str):
    if not _LIGHTNING_AVAILABLE:
        return None
    for s in Teamspace.studios:
        if getattr(s, "name", None) == name and getattr(s, "status", None) == "Running":
            return s
    return None


def reuse_or_create_studio(name: str, machine: str = "Lightning.L40S"):
    """
    Reuse a running studio if present; otherwise create one.
    If studio exists but is stopped (e.g. idle timeout), restart it.
    """
    if not _LIGHTNING_AVAILABLE:
        print("[dry-run] Lightning not available; skipping studio management.")
        return None

    studio = find_running_studio(name)
    if studio:
       
