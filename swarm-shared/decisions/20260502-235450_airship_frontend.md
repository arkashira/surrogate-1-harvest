# airship / frontend

## Highest-Value Incremental Improvement
**Deterministic CDN-only HF Dataset Manifest Generator**  
A CLI + HTTP endpoint that produces a deterministic JSON manifest of public HF dataset files (date-scoped) so Lightning training can fetch via CDN without any HF API calls during data loading. Unblocks surrogate-1 training and bypasses 429 rate limits.

**Why this now**:  
- Directly applies the *HF CDN Bypass* pattern (THE KEY INSIGHT 2026-04-29).  
- Enables Lightning Studio reuse and avoids idle-stop training deaths by giving training scripts a static file list.  
- Ships in <2h (single Python module + one FastAPI route + minimal config).

---

## Implementation Plan (≤2h)

| Step | Owner | Time | Details |
|------|-------|------|---------|
| 1 | Engineer | 15m | Add `requirements` (requests, python-multipart if needed) |
| 2 | Engineer | 30m | Create `tools/hf_cdn_manifest.py` — CLI to list repo tree for one date folder and emit `manifest.json` |
| 3 | Engineer | 20m | Add FastAPI route `GET /hf-manifest/{repo}/{date}` that returns CDN URLs + sizes + etag hints |
| 4 | Engineer | 15m | Wire into `surrogate` service Dockerfile (copy tool, install deps) |
| 5 | Engineer | 20m | Update surrogate README snippet with usage and Lightning training integration note |
| 6 | QA | 20m | Smoke test: generate manifest for a known date folder, verify CDN URLs resolve (HEAD 200) |

---

## Code Snippets

### 1. `tools/hf_cdn_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate a deterministic CDN-only manifest for a HuggingFace dataset date folder.
Usage:
  python tools/hf_cdn_manifest.py \
    --repo datasets/my-org/surrogate-1 \
    --date 2026-04-29 \
    --out manifest.json
"""
import argparse
import json
import os
import sys
from typing import List, Dict

import requests

CDN_BASE = "https://huggingface.co"
API_BASE = "https://huggingface.co/api"

def list_date_folder(repo: str, date: str) -> List[Dict]:
    """
    Single API call to list files in {date} folder (non-recursive by default).
    Falls back to raw tree endpoint if needed.
    """
    path = f"datasets/{repo}/tree/main/{date}"
    url = f"{API_BASE}/{path}"
    resp = requests.get(url, timeout=30)
    if resp.status_code == 404:
        # Try recursive=False explicit
        url = f"{API_BASE}/datasets/{repo}/tree?recursive=false&path={date}"
        resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()

def build_manifest(repo: str, date: str) -> Dict:
    entries = list_date_folder(repo, date)
    files = []
    for item in entries:
        if item.get("type") != "file":
            continue
        # CDN URL (no auth, no API rate limit)
        cdn_url = f"{CDN_BASE}/datasets/{repo}/resolve/main/{date}/{item['path']}"
        files.append(
            {
                "path": item["path"],
                "cdn_url": cdn_url,
                "size": item.get("size"),
                "lfs": item.get("lfs"),
                "oid": item.get("oid"),
            }
        )

    # Deterministic ordering
    files.sort(key=lambda f: f["path"])

    manifest = {
        "repo": repo,
        "date": date,
        "generated_by": "hf_cdn_manifest",
        "count": len(files),
        "files": files,
    }
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate HF CDN manifest for a date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., my-org/surrogate-1)")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-04-29)")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()

    try:
        manifest = build_manifest(args.repo, args.date)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    print(f"Wrote {len(manifest['files'])} files to {args.out}")

if __name__ == "__main__":
    main()
```

### 2. FastAPI route in `surrogate/api/routes/hf.py`
```python
from fastapi import APIRouter, HTTPException
from tools.hf_cdn_manifest import build_manifest

router = APIRouter(prefix="/hf-manifest", tags=["hf-cdn"])

@router.get("/{repo}/{date}")
def get_hf_manifest(repo: str, date: str):
    """
    Deterministic CDN-only manifest for a HuggingFace dataset date folder.
    Example: GET /hf-manifest/datasets/my-org/surrogate-1/2026-04-29
    """
    try:
        manifest = build_manifest(repo, date)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return manifest
```

### 3. Lightning training usage note (add to surrogate README)
```markdown
## Lightning Training with CDN-only Manifest

1. On your Mac (or orchestrator), generate manifest after rate-limit window clears:
   ```bash
   python tools/hf_cdn_manifest.py --repo datasets/my-org/surrogate-1 --date 2026-04-29 --out manifest.json
   ```

2. Embed `manifest.json` in your Lightning training script. Data loader should fetch via `cdn_url` only (zero HF API calls).

3. Reuse running studios to save quota:
   ```python
   from lightning import Teamspace
   for s in Teamspace.studios():
       if s.name == "surrogate-train" and s.status == "running":
           studio = s
           break
   else:
       studio = Studio(create_ok=True)
   ```

4. If studio was stopped (idle timeout), restart before `.run()`:
   ```python
   if studio.status != "running":
       studio.start(machine=Machine.L40S)
   ```
```

---

## Verification (smoke test)
```bash
# Generate
python tools/hf_cdn_manifest.py --repo datasets/airship/surrogate-1 --date 2026-04-29 --out /tmp/manifest.json

# Check CDN reachability (HEAD, no auth)
head -n 20 /tmp/manifest.json | jq -r '.files[].cdn_url' | xargs -I{} curl -s -o /dev/null -w "%{url} %{http_code}\n" -I {}
```
Expect `200` for all CDN URLs.

---

## Tags
#hf-cdn #manifest #surrogate-1 #lightning-ai #rate-limit-bypass #frontend-adjacent
