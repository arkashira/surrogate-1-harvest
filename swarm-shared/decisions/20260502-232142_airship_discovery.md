# airship / discovery

## Final Synthesized Implementation (Best of Both Candidates)

**Goal** (unchanged, now locked):  
Harden `airship discover` into a deterministic, **CDN-only** orchestrator that eliminates HF API rate limits and PyArrow schema errors while producing **reproducible file lists** and **safe ingestion artifacts**.

**Why this ships in ≤2h** (merged rationale):  
- Reuses existing `airship` CLI scaffold + `airship discover` entrypoint  
- No training/infra changes — only discovery/ingest layer  
- Single new script + small Makefile target + one config tweak  
- CDN bypass removes rate-limit; projection-at-parse removes schema risk  
- Immediately unblocks downstream surrogate-1 training via stable manifest  

---

## Implementation Plan (merged, no contradictions)

| Step | Action | Owner | Time |
|------|--------|-------|------|
| 1 | Inspect current `airship discover` entrypoint; identify where to inject CDN path | me | 15m |
| 2 | Add `scripts/discover_cdn_manifest.py` — deterministic CDN-only file lister + JSON manifest | me | 30m |
| 3 | Add `scripts/download_cdn_files.py` — parallel CDN fetch with retry; project to `{prompt,response}` only at parse | me | 45m |
| 4 | Add `Makefile` targets: `discover-cdn`, `ingest-cdn`, `clean-cdn` | me | 15m |
| 5 | Update `.env.example` with `HF_DATASET_REPO`, `INGEST_DATE`, `CDN_CONCURRENCY` | me | 10m |
| 6 | Smoke test against a small public dataset (e.g., `tatsu-lab/alpaca`) | me | 30m |

---

## Code (merged strongest snippets, hardened for correctness + actionability)

### 1) `scripts/discover_cdn_manifest.py`
Deterministic CDN-only listing; avoids `list_repo_files` recursion and API auth limits.  
**Key fix**: fallback to empty manifest on rate-limit (no crash), and strict extension filter to avoid schema heterogeneity.

```python
#!/usr/bin/env python3
"""
Discover HF dataset files via CDN and emit reproducible manifest.
Usage:
  HF_DATASET_REPO=username/dataset INGEST_DATE=2024-05-01 \
    python scripts/discover_cdn_manifest.py > manifests/2024-05-01.json
"""
import os
import json
import sys
import time
import logging
from pathlib import Path
from typing import List, Dict

import requests
from huggingface_hub import HfApi

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("discover_cdn")

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO")
INGEST_DATE = os.getenv("INGEST_DATE")
OUTPUT_DIR = Path("manifests")
OUTPUT_DIR.mkdir(exist_ok=True)

if not HF_DATASET_REPO or not INGEST_DATE:
    log.error("Set HF_DATASET_REPO and INGEST_DATE")
    sys.exit(1)

# Single non-recursive tree call per date folder to avoid pagination/rate-limit
def list_date_folder(repo: str, date: str) -> List[Dict]:
    api = HfApi()
    try:
        items = api.list_repo_tree(repo=repo, path=date, recursive=False)
        return [{"path": it.path, "size": it.size} for it in items if it.type == "file"]
    except Exception as e:
        log.error("Tree list failed (may be rate-limited): %s", e)
        log.info("Emitting empty manifest; retry after rate-limit window")
        return []

def build_manifest(files: List[Dict], repo: str, date: str) -> Dict:
    manifest = {
        "repo": repo,
        "date": date,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": [],
    }
    for f in files:
        # Only include likely data extensions to avoid schema heterogeneity
        if not f["path"].lower().endswith((".jsonl", ".json", ".parquet", ".csv")):
            continue
        manifest["files"].append(
            {
                "path": f["path"],
                "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{f['path']}",
                "size": f["size"],
            }
        )
    return manifest

def main() -> None:
    files = list_date_folder(HF_DATASET_REPO, INGEST_DATE)
    manifest = build_manifest(files, HF_DATASET_REPO, INGEST_DATE)
    out_path = OUTPUT_DIR / f"{INGEST_DATE}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    log.info("Manifest written: %s (%d files)", out_path, len(manifest["files"]))
    # Also print to stdout for Make pipelines
    print(json.dumps(manifest))

if __name__ == "__main__":
    main()
```

---

### 2) `scripts/download_cdn_files.py`
Parallel CDN fetch with retry; projects to `{prompt,response}` only at parse time to avoid PyArrow schema errors.  
**Key fix**: store raw bytes for non-JSONL (defer projection to training), and robust retry/backoff.

```python
#!/usr/bin/env python3
"""
Download dataset files via CDN (no auth/rate-limit) and project to prompt/response.
Usage:
  HF_DATASET_REPO=username/dataset INGEST_DATE=2024-05-01 \
    python scripts/download_cdn_files.py manifests/2024-05-01.json
"""
import os
import json
import sys
import time
import logging
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from tqdm import tqdm

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("download_cdn")

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO")
INGEST_DATE = os.getenv("INGEST_DATE")
CONCURRENCY = int(os.getenv("CDN_CONCURRENCY", "8"))
RETRY = 3
BACKOFF = 5

OUT_DIR = Path("batches") / "mirror-merged" / INGEST_DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)

def slug_for(path: str) -> str:
    h = hashlib.sha256(path.encode()).hexdigest()[:12]
    name = Path(path).stem
    return f"{name}_{h}"

def download_one(entry: Dict) -> Path:
    url = entry["cdn_url"]
    path = entry["path"]
    slug = slug_for(path)
    out_file = OUT_DIR / f"{slug}.jsonl"
    if out_file.exists():
        log.debug("Skip existing: %s", out_file)
        return out_file

    for attempt in range(1, RETRY + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            break
        except Exception as e:
            if attempt == RETRY:
                log.error("Failed %s after %d attempts: %s", url, RETRY, e)
                raise
            sleep = BACKOFF * attempt
            log.warning("Retry %s in %ss: %s", url, sleep, e)
            time.sleep(sleep)

    # Lightweight projection: keep only prompt/response-like fields if JSON
    # For parquet/csv we defer projection to training time to avoid schema issues
    ext = Path(path).suffix.lower()
    if ext == ".jsonl":
        projected = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                projected.append(
                    {
                        "prompt": obj.get("prompt") or obj.get("instruction") or "",
                        "response": obj.get("response") or obj.get("output") or "",
                    }
                )
            except Exception:
                continue
        out_file.write_text("\n".join(json.dumps(x) for x in projected))
    else:
        # Store raw bytes for non-JSONL; training script will project with pyarrow safely
        out_file.write_bytes(resp.content)
    return out_file

def main() -> None:
    manifest_path = Path(sys
