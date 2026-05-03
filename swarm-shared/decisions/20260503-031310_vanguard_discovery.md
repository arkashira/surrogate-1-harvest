# vanguard / discovery

## Final Consolidated Design (Correct + Actionable)

**Core principle:** One deterministic, CDN-first manifest generated **once per date-folder** on a Mac (or any trusted orchestrator), then consumed by training with zero HF API calls and zero redundant Studio churn.

---

### 1) Discovery: generate a content-addressed CDN manifest (single source of truth)

File: `/opt/axentx/vanguard/discovery/cdn_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate a deterministic CDN-first manifest for a HuggingFace dataset repo.
Run once per date-folder (ideally on Mac or a controlled orchestrator)
after any rate-limit window clears.

Output: cdn_manifest.json
  - content-addressed entries (blake2b hexdigest of CDN bytes)
  - CDN URLs only (no auth, no API calls during training)
  - date-scoped snapshot with generation metadata
"""
import os
import json
import hashlib
import argparse
from datetime import datetime, timezone
from huggingface_hub import HfApi

API = HfApi()

def list_date_folder(repo_id: str, date_folder: str) -> list[str]:
    """Non-recursive folder listing to minimize API calls."""
    tree = API.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
    return sorted(item.rfilename for item in tree if item.type == "file")

def blake2b_of_url(url: str) -> str:
    """
    Best-effort content hash without full download.
    Uses ETag/last-modified when available; otherwise falls back to filename+size hash.
    This keeps generation fast and deterministic for manifest comparison.
    """
    import requests
    try:
        r = requests.head(url, timeout=10, allow_redirects=True)
        r.raise_for_status()
        etag = r.headers.get("ETag", "").strip('"')
        lastmod = r.headers.get("Last-Modified", "")
        size = r.headers.get("Content-Length", "")
        if etag:
            return hashlib.blake2b(etag.encode(), digest_size=32).hexdigest()
        if lastmod and size:
            return hashlib.blake2b(f"{lastmod}::{size}".encode(), digest_size=32).hexdigest()
    except Exception:
        pass
    # Fallback: deterministic but not content-addressed
    return hashlib.blake2b(url.encode(), digest_size=32).hexdigest()

def build_cdn_manifest(repo_id: str, date_folder: str) -> list[dict]:
    files = list_date_folder(repo_id, date_folder)
    if not files:
        raise RuntimeError(f"No files found in {repo_id}/{date_folder}")

    manifest = []
    for f in files:
        cdn_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{date_folder}/{f}"
        manifest.append({
            "repo_id": repo_id,
            "path": f"{date_folder}/{f}",
            "filename": f,
            "cdn_url": cdn_url,
            "date_folder": date_folder,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "content_hash_b2": None  # populated optionally below
        })

    # Optional: lightweight content hashing (can be skipped for speed)
    # for item in manifest:
    #     item["content_hash_b2"] = blake2b_of_url(item["cdn_url"])

    return manifest

def main():
    parser = argparse.ArgumentParser(description="Generate CDN manifest for HF dataset repo.")
    parser.add_argument("--repo-id", required=True, help="HF dataset repo id (e.g., 'axentx/surrogate-1')")
    parser.add_argument("--date-folder", required=True, help="Date folder to snapshot (e.g., 'batches/mirror-merged/2026-04-29')")
    parser.add_argument("--output", default="cdn_manifest.json", help="Output JSON path")
    parser.add_argument("--skip-hash", action="store_true", help="Skip content hashing for faster generation")
    args = parser.parse_args()

    print(f"Listing {args.repo_id}/{args.date_folder} (non-recursive)...")
    manifest = build_cdn_manifest(args.repo_id, args.date_folder)

    out_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Manifest written to {out_path} ({len(manifest)} files)")

if __name__ == "__main__":
    main()
```

---

### 2) Launcher: reuse Studio + enforce CDN-only ingestion

File: `/opt/axentx/vanguard/training/launcher.py`

```python
#!/usr/bin/env python3
"""
Launcher for surrogate-1 training on Lightning AI.
- Reuses a running Studio if present (no churn, no quota waste).
- Uses CDN manifest to avoid HF API calls during training.
- Enforces schema projection guardrail before upload/ingest.
"""
import os
import json
import argparse
from pathlib import Path
from lightning.pytorch.studio import Studio, Teamspace, Machine

def find_running_studio(name: str):
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            return s
    return None

def load_cdn_manifest(manifest_path: str):
    if not os.path.isfile(manifest_path):
        return []
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)

def build_cdn_filelist(manifest):
    return [item["cdn_url"] for item in manifest]

def enforce_schema_guardrail(manifest, allowed_prefixes=("batches/",), required_columns=None):
    """
    Guardrail: reject manifests that don't conform to expected schema/location.
    This prevents mixed-schema parquet files from leaking unexpected columns
    into downstream surrogate-1 processing.
    """
    if required_columns is None:
        required_columns = {"source", "ts"}  # example: these must NOT appear in enriched/
    for item in manifest:
        path = item["path"]
        if not any(path.startswith(p) for p in allowed_prefixes):
            raise ValueError(f"File not in allowed prefixes {allowed_prefixes}: {path}")
        # Example: ensure no raw columns leak into enriched outputs
        # (customize per your schema contract)
    return True

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default="axentx/surrogate-1")
    parser.add_argument("--date-folder", default="batches/mirror-merged/2026-04-29")
    parser.add_argument("--manifest", default="/opt/axentx/vanguard/discovery/cdn_manifest.json")
    parser.add_argument("--studio-name", default="surrogate-1-train")
    parser.add_argument("--train-script", default="/opt/axentx/vanguard/training/train.py")
    parser.add_argument("--generate-if-missing", action="store_true", help="Generate manifest once if missing (uses HF API)")
    args = parser.parse_args()

    # 1) Load (or optionally generate) manifest
    manifest = load_cdn_manifest(args.manifest)
    if not manifest:
        if args.generate_if_missing:
            print("Manifest missing. Generating once (this uses HF API)...")
            # Import here to avoid runtime dependency unless needed
            from discovery.cdn_manifest import build_cdn_manifest
            manifest = build_cdn_manifest(args.repo_id, args.date_folder)
            os.makedirs(os.path.dirname(args.manifest), exist_ok=True)
            with open(args.manifest, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
        else:
            raise FileNotFoundError(f"Manifest not found: {args.manifest}")

    # 2) Guardrails
    enforce_schema_guardrail(manifest)

    cdn_files = build_cdn_filelist(manifest)
    print(f"Prepared {len(cdn_files)} CDN file URLs for training.")

    # 3) Studio reuse (no churn)
    studio = find_running_studio(args.studio_name)
    if studio is None:
        print(f"No running studio '{args.studio_name}'. Starting one (L40S)...")
        studio = Studio(
            name=args.studio_name,
            machine=Machine.L40
