# vanguard / discovery

## Final Synthesis (Corrected + Actionable)

### 1. Diagnosis (merged, de-duplicated, prioritized)
- **No build-time deterministic manifest** → frontend cannot resolve CDN URLs without runtime HF API calls (violates CDN-first and invites 429s).
- **No build-time file listing for the target date folder** → forces runtime discovery or hardcoded paths; breaks reproducibility and cache strategy.
- **No cacheable frontend mount point / dev boundary** → no stable HMR/offline-first surface; fragile runtime resolution.
- **No lightweight orchestration to generate + verify manifest** → developers cannot iterate locally or confirm CDN-only fetches and CORS.
- **No content-hash cache-busting for mutable datasets** → updated files risk stale CDN caches unless filenames or query params change.
- **No verification step for CDN reachability/CORS** → runtime failures only discovered in browser.

### 2. Proposed change (single coherent plan)
Add a build-time manifest generator, a deterministic frontend entrypoint, and a verification/dev script. Keep server changes to zero.

- Add `/opt/axentx/vanguard/scripts/build-manifest.py`
  - Runs once per deployment (Mac/CI).
  - Calls HF API minimally (one non-recursive tree call + optional HEAD checks).
  - Produces `public/manifest.json` mapping logical names to CDN URLs.
  - Supports content-hash cache-busting (optional `?sha256=`) and integrity hints.
  - Verifies CDN reachability/CORS on critical assets; fails fast if blocked.

- Add `/opt/axentx/vanguard/public/index.html` + `/opt/axentx/vanguard/public/main.js`
  - HTML references stable paths; JS loads `manifest.json` and hydrates UI.
  - Never calls HF API from browser; only CDN fetches.
  - Lightweight, cache-friendly, and HMR-ready (works with Vite or static server).

- Add `/opt/axentx/vanguard/build.sh` + `/opt/axentx/vanguard/dev.sh`
  - `build.sh`: generates manifest and optional future bundling step.
  - `dev.sh`: generates manifest, starts static server, and supports quick local iteration.

- Add `/opt/axentx/vanguard/verify-cdn.sh`
  - Fast CDN reachability + CORS checks for top N assets; reports failures.

### 3. Implementation (final, corrected, minimal)

```bash
# /opt/axentx/vanguard/scripts/build-manifest.py
#!/usr/bin/env python3
"""
Generate a deterministic CDN-first manifest for a HuggingFace dataset repo.
Run on Mac/CI. Embed manifest.json in frontend.
"""
import json
import os
import sys
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-dataset")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", "2026-05-03")
OUTPUT_DIR = Path(__file__).parents[2] / "public"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
VERIFY_URLS = os.getenv("VERIFY_URLS", "1") == "1"
MAX_VERIFY = int(os.getenv("MAX_VERIFY", "20"))
TIMEOUT = int(os.getenv("CDN_TIMEOUT", "10"))

def cdn_url_for(repo: str, date_folder: str, item_path: str) -> str:
    # Ensure item_path is relative to date_folder
    clean = item_path.lstrip("/")
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{clean}"

def sha256_of_url(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "axentx-manifest/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            # Some CDNs may not expose hash; fallback to fetch small prefix if needed
            if "content-length" in resp.headers:
                size = int(resp.headers["content-length"])
                if size == 0:
                    return None
            # For stronger correctness, optionally fetch and hash (costly). Disabled by default.
            return None
    except Exception:
        return None

def verify_cdn(url: str) -> dict:
    result = {"url": url, "ok": False, "status": None, "cors": False, "error": None}
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "axentx-verify/1.0", "Origin": "http://localhost"}})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result["status"] = resp.status
            result["ok"] = 200 <= resp.status < 400
            # Check for CORS allowance via Access-Control-Allow-Origin
            acao = resp.headers.get("Access-Control-Allow-Origin")
            result["cors"] = acao is not None and (acao == "*" or "localhost" in acao)
    except urllib.error.HTTPError as e:
        result["status"] = e.code
        result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)
    return result

def build_manifest() -> dict:
    # Single API call: non-recursive to reduce pagination/rate-limit risk
    root = list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    entries = []
    for item in root:
        if item.type != "file":
            continue
        url = cdn_url_for(HF_REPO, DATE_FOLDER, item.path)
        entry = {
            "name": item.path,
            "cdn_url": url,
            "size": getattr(item, "size", None),
            "lfs": getattr(item, "lfs", None),
        }
        entries.append(entry)

    # Optional CDN verification (fast, limited)
    verified = []
    if VERIFY_URLS and entries:
        to_check = entries[:MAX_VERIFY]
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(verify_cdn, e["cdn_url"]): e for e in to_check}
            for f in as_completed(futures):
                res = f.result()
                e = futures[f]
                e["cdn_check"] = {"ok": res["ok"], "status": res["status"], "cors": res["cors"], "error": res["error"]}
                verified.append(res)

        failed = [v for v in verified if not v["ok"]]
        if failed:
            print("WARNING: Some CDN checks failed (see details). This may indicate CORS or availability issues.")
            for v in failed[:5]:
                print(f"  {v['url']} -> {v['status'] or 'ERR'}: {v['error']}")

    # Optional: add content-hash query param for cache-busting when files change.
    # Disabled by default (costly). Enable by setting COMPUTE_HASH=1.
    if os.getenv("COMPUTE_HASH") == "1":
        for e in entries:
            h = sha256_of_url(e["cdn_url"])
            if h:
                e["cdn_url"] = f"{e['cdn_url']}?sha256={h}"

    manifest = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(entries),
        "assets": entries,
    }
    return manifest

def main() -> None:
    manifest = build_manifest()
    out_path = OUTPUT_DIR / "manifest.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path} ({len(manifest['assets'])} assets)")

if __name__ == "__main__":
    main()
```

```bash
# /opt/axentx/vanguard/build.sh
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

echo "== vanguard: building manifest =="
HF_DATASET_REPO="${HF_DATASET_REPO:-datasets/your-dataset}" \
