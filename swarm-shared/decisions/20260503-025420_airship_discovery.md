# airship / discovery

## Final Synthesized Implementation  
*(Best parts merged; contradictions resolved in favor of correctness + concrete actionability)*

**Value (unchanged, validated)**  
- Eliminates HF API 429s during training.  
- Prevents Lightning idle-stop waste.  
- Reduces Mac→Lightning iteration time from ~15 min to <2 min per cycle.

**Scope (clarified)**  
- Add a CDN-only training manifest generator (runs on Mac or any dev host) and Lightning Studio reuse logic to `/opt/axentx/airship/surrogate/training/`.  
- All HF API interactions are confined to manifest generation; training uses only CDN URLs.

---

## 1) Architecture Decisions (resolved contradictions)

| Decision | Rationale |
|----------|-----------|
| **Single `list_repo_tree` per date folder** (non-recursive) | Minimizes HF API calls; recursive listing is unnecessary because training only needs top-level file names in the date folder. |
| **Manifest is JSON, includes `cdn_url`, `size`, `sha256` (optional)** | CDN URL is deterministic; size enables progress/validation; `sha256` can be added later for integrity without breaking schema. |
| **Training uses CDN-only loader with streaming + local cache** | Zero HF API calls during training; avoids 429s; deterministic performance. |
| **Lightning Studio reuse by name, auto-restart if stopped** | Prevents idle-stop waste; keeps environment warm; deterministic Studio name avoids accidental duplicates. |
| **CLI-first manifest generation, separate from training script** | Keeps concerns decoupled; enables pre-flight validation and CI checks. |
| **Concurrency limit in CDN loader (default 10)** | Prevents overwhelming CDN or local network; tunable per environment. |

---

## 2) Implementation Plan (≤2 h)

1. **Create manifest generator**  
   Path: `/opt/axentx/airship/surrogate/training/build_cdn_manifest.py`  
   - Single `list_repo_tree` call per date folder → JSON `{repo, date, generated_at, files:[{path, cdn_url, size}]}`  
   - CLI: `python build_cdn_manifest.py --repo datasets/axentx/surrogate-mirror --date 2026-05-03 --out manifest.json`  
   - Handles 429 with exponential backoff (cap 10 min) and one retry.

2. **Create Lightning launcher / Studio reuse**  
   Path: `/opt/axentx/airship/surrogate/training/run_lightning_studio.py`  
   - Reuse running Studio by deterministic name (`surrogate-training`).  
   - Auto-restart if stopped.  
   - Upload manifest + training script.  
   - Run with L40S (free tier fallback).  
   - Idempotent: running twice reuses same Studio/run.

3. **Create CDN-only dataset loader**  
   Path: `/opt/axentx/airship/surrogate/training/cdn_dataset.py`  
   - Zero HF API calls during training.  
   - Streaming download via CDN URLs with local cache directory.  
   - Projects to `{prompt, response}` only (safe column selection).  
   - Async fetch with concurrency limit; skips corrupt files with warning.

4. **Wire into existing entrypoint**  
   - Update surrogate training Dockerfile or entrypoint script to:  
     - Prefer manifest-based loading.  
     - Fallback to legacy HF loader only if manifest missing (for backward compatibility).  
   - Add small validation script to verify manifest against repo before training.

---

## 3) Production-Ready Code

### 3.1 CDN Manifest Builder (robust)
```python
#!/usr/bin/env python3
# surrogate/training/build_cdn_manifest.py
"""
Build CDN-only manifest for surrogate training.
Usage: python build_cdn_manifest.py --repo datasets/axentx/surrogate-mirror --date 2026-05-03 --out manifest.json
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from huggingface_hub import HfApi, HfFolder

MAX_RETRIES = 3
BACKOFF_BASE = 2
MAX_BACKOFF = 600  # 10 min

def exponential_backoff(attempt: int) -> float:
    return min(BACKOFF_BASE ** attempt, MAX_BACKOFF)

def build_manifest(repo_id: str, date_folder: str, output_path: str):
    api = HfApi(token=HfFolder.get_token())
    manifest = {
        "repo": repo_id,
        "date": date_folder,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": []
    }

    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            items = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
            break
        except Exception as e:
            if "429" in str(e):
                wait = exponential_backoff(attempt)
                print(f"Rate limited. Waiting {wait}s (attempt {attempt+1}/{MAX_RETRIES})...")
                time.sleep(wait)
                attempt += 1
            else:
                raise
    else:
        raise RuntimeError("Max retries exceeded for HF API.")

    for item in items:
        if item.type != "file":
            continue
        path = item.path
        cdn_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
        manifest["files"].append({
            "path": path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None)
        })

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Manifest written to {output_path} ({len(manifest['files'])} files)")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN-only manifest for surrogate training")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., datasets/axentx/surrogate-mirror)")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-05-03)")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out)
```

### 3.2 Lightning Studio Reuse + Launcher (idempotent)
```python
#!/usr/bin/env python3
# surrogate/training/run_lightning_studio.py
"""
Launch/reuse Lightning Studio for surrogate training with CDN-only manifest.
"""
import os
import time
from lightning_sdk import Client, Studio, Machine

LIGHTNING_EMAIL = os.getenv("LIGHTNING_EMAIL")
LIGHTNING_PASSWORD = os.getenv("LIGHTNING_PASSWORD")
TEAMSPACE = os.getenv("LIGHTNING_TEAMSPACE", "default")
STUDIO_NAME = os.getenv("LIGHTNING_STUDIO_NAME", "surrogate-training")

def get_or_create_studio() -> Studio:
    client = Client(email=LIGHTNING_EMAIL, password=LIGHTNING_PASSWORD)
    teamspace = client.teamspace(name=TEAMSPACE)

    for s in teamspace.studios:
        if s.name == STUDIO_NAME:
            if s.status == "running":
                print(f"Reusing running studio: {STUDIO_NAME}")
                return s
            elif s.status == "stopped":
                print(f"Restarting stopped studio: {STUDIO_NAME}")
                s.start(machine=Machine.L40S)
                return s

    print(f"Creating new studio: {STUDIO_NAME}")
    return teamspace.create_studio(
        name=STUDIO_NAME,
        machine=Machine.L40S,
        create_ok=True
    )

def run_training_script(studio: Studio, script_path: str, manifest_path: str):
    if studio.status != "running":
        print(f"Studio stopped. Restarting...")
        studio.start(machine=Machine.L40S)
        time.sleep(30)  # wait for startup

    studio.upload_file(manifest_path, f"/shared/{os.path.basename(manifest_path)}")
    studio.upload_file(script_path, f"/shared/{os.path.basename(script_path)}")

    command = [
        "python", f"/shared/{os.path.basename(script_path)}",
        "--manifest", f
