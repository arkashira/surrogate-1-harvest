# vanguard / discovery

## Final Synthesis (single, actionable plan)

**Core problem**: repeated HF API calls and Lightning quota burn during discovery/training; schema drift and no CDN bypass.  
**North-star goal**: one-shot, rate-limit-safe discovery that produces a reusable manifest + CDN-backed file list and keeps a single Lightning Studio alive across runs.

---

## 1. What to build (minimal, high-leverage)

- **One new module**: `/opt/axentx/vanguard/scripts/discovery_manifest.py`  
  - Single non-recursive `list_repo_tree` per run (avoids pagination/429).  
  - Projects each file to `{prompt, response}` (drops unknown columns → schema-drift safe).  
  - Writes two artifacts:  
    - `manifests/manifest_YYYY-MM-DD.json` (full metadata + CDN URLs).  
    - `manifests/filelist_YYYY-MM-DD.json` (compact path list for training).  
  - Uses public CDN URLs (`resolve/main/...`) so training can bypass HF API entirely.  

- **One wrapper**: `/opt/axentx/vanguard/scripts/run_discovery.sh` (Bash, `set -euo pipefail`, shebang).  

- **Small integration patch**: update any launcher/trainer to load `filelist_*.json` instead of calling HF APIs; use CDN URLs for data loading.

- **Lightning reuse guard**:  
  - Reuse a running Studio; if idle-stopped, restart; create only if absent.  
  - Prevents quota burn from create/idle/stop churn.  

- **Running-state check before `.run()`**: ensure Studio is actually running before kicking off training (prevents idle-stop kills).

---

## 2. Implementation (canonical version)

```bash
# /opt/axentx/vanguard/scripts/run_discovery.sh
#!/usr/bin/env bash
set -euo pipefail
export SHELL=/bin/bash
cd /opt/axentx/vanguard
exec python3 scripts/discovery_manifest.py "$@"
```

```python
# /opt/axentx/vanguard/scripts/discovery_manifest.py
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from huggingface_hub import HfApi, hf_hub_download, list_repo_tree
from lightning import Studio, Teamspace

# ---------- CONFIG ----------
HF_REPO = os.getenv("HF_REPO", "datasets/axentx/vanguard-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
MANIFEST_DIR = Path(os.getenv("MANIFEST_DIR", "/opt/axentx/vanguard/manifests"))
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

MANIFEST_PATH = MANIFEST_DIR / f"manifest_{DATE_FOLDER}.json"
FILELIST_PATH = MANIFEST_DIR / f"filelist_{DATE_FOLDER}.json"

LIGHTNING_NAME = os.getenv("STUDIO_NAME", "vanguard-discovery")
MACHINE_TYPE = os.getenv("MACHINE_TYPE", "L40S")
# ----------------------------

def list_date_files(repo: str, date_folder: str) -> List[str]:
    items = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [it.path for it in items if it.type == "file"]
    return sorted(files)

def cdn_urls(repo: str, file_paths: List[str]) -> List[str]:
    base = f"https://huggingface.co/datasets/{repo}/resolve/main"
    return [f"{base}/{fp}" for fp in file_paths]

def project_file(file_path: str) -> Dict[str, Any]:
    local_path = hf_hub_download(repo_id=HF_REPO, filename=file_path)
    examples = []
    try:
        with open(local_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                examples.append({
                    "prompt": obj.get("prompt") or obj.get("input") or "",
                    "response": obj.get("response") or obj.get("output") or "",
                })
    except Exception as exc:
        print(f"Projection warning {file_path}: {exc}", file=sys.stderr)

    return {
        "file": file_path,
        "cdn_url": cdn_urls(HF_REPO, [file_path])[0],
        "num_examples": len(examples),
        "projected_at_utc": datetime.now(timezone.utc).isoformat(),
    }

def reuse_or_create_studio(name: str, machine_type: str) -> Studio:
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name:
            if s.status == "running":
                print(f"Reusing running studio: {name}")
                return s
            print(f"Studio {name} status={s.status}. Restarting...")
            s.start(machine=machine_type)
            return s
    print(f"Creating studio: {name} on {machine_type}")
    return Studio(name=name, machine=machine_type, create_ok=True)

def main() -> None:
    print(f"Building discovery manifest for {HF_REPO}/{DATE_FOLDER}")
    file_paths = list_date_files(HF_REPO, DATE_FOLDER)
    if not file_paths:
        print("No files found for date folder.", file=sys.stderr)
        sys.exit(1)

    entries = [project_file(fp) for fp in file_paths]

    manifest = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": entries,
        "cdn_base": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main",
        "total_files": len(entries),
    }

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    FILELIST_PATH.write_text(json.dumps(file_paths, indent=2))
    print(f"Manifest: {MANIFEST_PATH}")
    print(f"File list: {FILELIST_PATH}")

    studio = reuse_or_create_studio(LIGHTNING_NAME, MACHINE_TYPE)
    print(f"Studio ready: {studio.name} ({studio.status})")

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/scripts/run_discovery.sh
```

---

## 3. Integration patch (training side)

```diff
--- a/scripts/train_surrogate.py
+++ b/scripts/train_surrogate.py
@@ -1,10 +1,13 @@
 import json
 from pathlib import Path
+from datasets import load_dataset

-MANIFEST_DIR = Path("/opt/axentx/vanguard/manifests")
+MANIFEST_DIR = Path("/opt/axentx/vanguard/manifests")
 DATE_FOLDER = "2026-05-02"  # or param/env

-def list_files_via_api():
-    ...
+def load_file_list():
+    p = MANIFEST_DIR / f"filelist_{DATE_FOLDER}.json"
+    return json.loads(p.read_text())
+
+def load_data_cdn(file_paths):
+    # Use CDN-backed local/streaming load to avoid HF API during training.
+    # Example: load from local cache or stream via fsspec/http using CDN URLs.
+    cdn_base = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"
+    return load_dataset(
+        "json",
+        data_files=[f"{cdn_base}/{fp}" for fp in file_paths],
+        streaming=True,
+    )
```

---

## 4. Verification checklist (run once, then CI)

1. **Run discovery**:
   ```bash
   bash /opt/axentx/vanguard/scripts/run_discovery.sh
   ```
2. **Artifacts exist**:
   - `manifests/manifest_YYYY-MM-DD.json` with `cdn_url` fields.  
   - `manifests/filelist_YYYY-MM-DD.json` compact list.  
3. **CDN bypass**:
   - Pick any `cdn_url`; `curl -I <cdn_url>` returns 200 (no auth required).  
4. **Lightning reuse**:
   - Second run logs “Reusing running studio” (no new studio created).  
5.
