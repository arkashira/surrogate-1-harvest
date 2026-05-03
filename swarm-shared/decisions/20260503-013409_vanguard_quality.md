# vanguard / quality

# Final Synthesis

**Chosen approach:** Adopt Candidate 1’s architecture (separate, reusable `manifest/` package with `generate_manifest.py` + `load_manifest.py`) and integrate Candidate 2’s critical correctness fixes (explicit parquet filter, UTC timezone marker, robust CDN URL construction, and deterministic path handling). This yields a minimal, cacheable, CDN-only training path that eliminates HF API discovery during training and removes `list_repo_tree` calls from the hot path.

---

## 1. Diagnosis (resolved)

- **Problem:** Frontend/training calls authenticated `list_repo_tree` on every load, burning HF quota (1000/5min) and causing 429s.
- **Root cause:** No persisted `(repo, dateFolder) → file-list` manifest; training re-enumerates folders via API instead of using cached JSON.
- **Secondary risk:** Heterogeneous file schemas in `load_dataset(streaming=True)` can cause `pyarrow.CastError`; training fetches via authenticated API instead of public CDN.
- **No retry/backoff** for 429 during discovery; failures bubble to UI as generic errors.
- **Missing local artifact path** for deterministic, cacheable CI/CD and training runs.

**Resolution priority:** Correctness + concrete actionability.
- Fix: Generate a local manifest once (after rate-limit window) and use CDN-only URLs for all training data fetches.
- Scope: new files plus minimal, targeted changes to training data loader; no model code changes.

---

## 2. Proposed change (final)

- Add `/opt/axentx/vanguard/manifest/` with:
  - `generate_manifest.py` — run on Mac or CI after rate-limit window; single non-recursive `list_repo_tree` per `(repo, dateFolder)`; writes deterministic JSON.
  - `load_manifest.py` — training/UI loads local JSON; never calls HF API for path discovery.
- Update training launcher to accept `--manifest` and skip any `list_repo_tree` calls.
- Data loader uses only CDN URLs and filters to parquet (or required extensions) to avoid schema surprises and `pyarrow.CastError`.
- Add simple retry/backoff for CDN fetches and clear error if manifest is missing.

---

## 3. Implementation (final)

### 3.1 Manifest generator

```bash
# /opt/axentx/vanguard/manifest/generate_manifest.py
#!/usr/bin/env python3
"""
Generate a local manifest for (repo, dateFolder) to avoid HF API discovery during training.
Usage:
  python generate_manifest.py --repo datasets/axentx/surrogate-1 --date 2026-04-29 --out manifests/
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("ERROR: huggingface_hub not installed. Run: pip install huggingface_hub")
    sys.exit(1)


def _normalize_repo_id(repo_id: str) -> str:
    # Accepts datasets/axentx/surrogate-1 or axentx/surrogate-1
    parts = [p for p in repo_id.split("/") if p]
    if len(parts) == 2:
        return "/".join(parts)
    if len(parts) == 3 and parts[0] == "datasets":
        return "/".join(parts[1:])
    raise ValueError(f"Unrecognized repo_id format: {repo_id}")


def build_manifest(repo_id: str, date_folder: str, out_dir: str, extensions: tuple = (".parquet",)) -> str:
    api = HfApi()
    repo_id = _normalize_repo_id(repo_id)
    prefix = f"{date_folder.rstrip('/')}/"

    # Single non-recursive call per folder (avoids pagination and reduces quota use)
    entries = api.list_repo_tree(repo_id=repo_id, path=prefix, recursive=False)

    files = []
    for e in entries:
        if not isinstance(e, dict):
            # HF API may return objects; normalize
            e = getattr(e, "__dict__", {})
        if e.get("type") != "file":
            continue
        path = e.get("path", "")
        if not path:
            continue
        if extensions and not any(path.lower().endswith(ext) for ext in extensions):
            continue

        # CDN download URL (public; bypasses API auth/rate limits during training)
        cdn_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
        files.append({
            "path": path,
            "size": e.get("size"),
            "cdn_url": cdn_url,
        })

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(files),
        "extensions": extensions,
        "files": files,
    }

    out_path = Path(out_dir) / repo_id.replace("/", "_") / f"{date_folder}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path} ({len(files)} files)")
    return str(out_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HF folder manifest for CDN-only training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., datasets/axentx/surrogate-1)")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-04-29)")
    parser.add_argument("--out", default="manifests", help="Output directory (default: manifests)")
    parser.add_argument("--ext", nargs="+", default=[".parquet"], help="File extensions to include (default: .parquet)")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out, tuple(args.ext))
```

### 3.2 Manifest loader

```python
# /opt/axentx/vanguard/manifest/load_manifest.py
import json
from pathlib import Path
from typing import Dict, List, Optional


def load_manifest(repo_id: str, date_folder: str, manifests_root: str = "manifests") -> Optional[Dict]:
    # Normalize repo_id for filename consistency
    repo_key = repo_id.replace("/", "_").replace("datasets_", "")
    if not repo_key.startswith("axentx"):
        repo_key = f"axentx_{repo_key}"
    p = Path(manifests_root) / repo_key / f"{date_folder}.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def cdn_urls(manifest: Dict) -> List[str]:
    return [f["cdn_url"] for f in manifest.get("files", [])]


def parquet_urls(manifest: Dict) -> List[str]:
    return [f["cdn_url"] for f in manifest.get("files", []) if f["cdn_url"].lower().endswith(".parquet")]
```

### 3.3 Training integration (minimal, targeted)

```diff
# /opt/axentx/vanguard/train.py  (example integration)
# Add near top:
+ from manifest.load_manifest import load_manifest, parquet_urls
+ import urllib.request
+ import time

+ def robust_cdn_get(url: str, retries: int = 3, backoff: float = 1.0):
+     for attempt in range(1, retries + 1):
+         try:
+             with urllib.request.urlopen(url, timeout=30) as resp:
+                 return resp.read()
+         except Exception as exc:
+             if attempt == retries:
+                 raise
+             time.sleep(backoff * attempt)

# Replace any HF API folder enumeration with:
-   entries = api.list_repo_tree(...)
+   manifest = load_manifest(repo_id, date_folder, manifests_root="manifests")
+   if not manifest:
+       raise FileNotFoundError(
+           f"Manifest missing for {repo_id}/{date_folder}. "
+           f"Run: python manifest/generate_manifest.py --repo {repo_id} --date {date_folder} --out manifests"
+       )
+   file_urls = parquet_urls(manifest)
+   # Use CDN URLs directly (no auth/rate-limit pressure)
```

### 3.4 Ops hygiene
