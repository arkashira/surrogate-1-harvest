# vanguard / backend

## Final Synthesis (adopts strongest, most actionable parts; resolves contradictions in favor of correctness + concrete execution)

**Core diagnosis (merged, de-duplicated)**
- No canonical discovery/entrypoint for backend services → onboarding and ops are ad-hoc; violates `#knowledge-rag #graph #hub` pattern (no MOC/hub review before planning).
- Missing HF CDN-bypass file-list generation for surrogate-1 training → future training jobs will hit 429 rate limits during data loading.
- No Lightning Studio reuse guard → each training run risks quota waste and idle-stop kills training.
- No surrogate-1 ingestion projection → dataset-mirror writes mixed-schema files to `enriched/` instead of projecting to `{prompt,response}` and using `batches/mirror-merged/{date}/{slug}.parquet`.
- Mac=CLI rule not enforced → orchestration may attempt local `model.from_pretrained()` or heavy compute instead of delegating to Lightning/Kaggle/Cerebras.

**Proposed change (merged, minimal, executable)**
Add a backend orchestration module that:
- Queries top hub (MOC) via knowledge-rag before planning (single CLI flag).
- Generates and embeds HF CDN file-list for surrogate-1 training (one API call per date folder, JSON artifact).
- Reuses a running Lightning Studio or restarts safely; guards against idle-stop.
- Projects mirror ingestion to `{prompt,response}` and writes to `batches/mirror-merged/{date}/{slug}.parquet`.
- Enforces Mac=CLI rule: never run heavy local model ops; delegate to Lightning/Kaggle/Cerebras.

Scope:
- New file: `/opt/axentx/vanguard/backend/orchestrate.py`
- New file: `/opt/axentx/vanguard/backend/lightning_studio.py`
- New file: `/opt/axentx/vanguard/backend/hf_cdn_filelist.py`
- Update: add CLI entrypoint script `/opt/axentx/vanguard/bin/vanguard-ctl` (executable, Bash shebang).

---

### Setup
```bash
mkdir -p /opt/axentx/vanguard/{backend,bin}
```

---

### `/opt/axentx/vanguard/backend/hf_cdn_filelist.py`
(Kept Candidate 1 implementation; correct, minimal, CDN-only.)
```python
import json
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict

from huggingface_hub import list_repo_tree

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/surrogate-1")
CDN_PREFIX = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

def list_date_folder(date_folder: str, repo: str = HF_REPO) -> List[Dict]:
    """
    Single API call: list non-recursive for one date folder.
    Returns items with cdn_url and local_path.
    """
    items = list_repo_tree(path=date_folder, repo_id=repo, recursive=False)
    out = []
    for it in items:
        if it.type != "file":
            continue
        cdn_url = f"{CDN_PREFIX}/{it.path}"
        out.append({
            "path": it.path,
            "cdn_url": cdn_url,
            "size": getattr(it, "size", None),
            "lfs": getattr(it, "lfs", None)
        })
    return out

def save_filelist(date_folder: str, out_dir: str = "filelists") -> str:
    os.makedirs(out_dir, exist_ok=True)
    files = list_date_folder(date_folder)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    name = f"{date_folder.replace('/', '_')}_{stamp}.json"
    p = Path(out_dir) / name
    p.write_text(json.dumps({"date_folder": date_folder, "generated": stamp, "files": files}, indent=2))
    return str(p)

if __name__ == "__main__":
    import sys
    folder = sys.argv[1] if len(sys.argv) > 1 else f"batches/mirror-merged/{datetime.utcnow().strftime('%Y/%m/%d')}"
    print(save_filelist(folder))
```

---

### `/opt/axentx/vanguard/backend/lightning_studio.py`
(Kept Candidate 1 implementation; correct, minimal, guards against idle-stop.)
```python
import time
import os
from lightning import Studio, Machine, Teamspace

LIGHTNING_ACCOUNT = os.getenv("LIGHTNING_ACCOUNT", "lightning-public-prod")
STUDIO_NAME = os.getenv("VANGUARD_STUDIO_NAME", "vanguard-surrogate-train")

def get_running_studio() -> Studio | None:
    for s in Teamspace.studios:
        if s.name == STUDIO_NAME and s.status == "Running":
            return s
    return None

def ensure_studio(machine: Machine = Machine.L40S) -> Studio:
    studio = get_running_studio()
    if studio:
        return studio
    return Studio(
        name=STUDIO_NAME,
        machine=machine,
        cloud_account=LIGHTNING_ACCOUNT,
        create_ok=True
    )

def run_with_reuse(target, machine: Machine = Machine.L40S, max_retries: int = 3):
    """
    Guard against idle-stop: check status and restart if stopped.
    """
    for attempt in range(1, max_retries + 1):
        studio = ensure_studio(machine=machine)
        if studio.status != "Running":
            studio.start(machine=machine)
            time.sleep(10)
        try:
            return studio.run(target)
        except Exception as exc:
            if attempt == max_retries:
                raise
            # idle-stop likely killed training; restart
            studio.stop()
            time.sleep(15)
            continue
```

---

### `/opt/axentx/vanguard/backend/orchestrate.py`
(Merged strongest parts; fixed imports; added Mac=CLI enforcement; made train_surrogate actually call run_with_reuse.)
```python
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from hf_cdn_filelist import save_filelist
from lightning_studio import run_with_reuse, ensure_studio

VANGUARD_ROOT = Path(__file__).parent.parent.parent

def enforce_mac_cli():
    """
    Mac=CLI rule: never run heavy local model ops on Mac.
    If on macOS, require delegation to Lightning/Kaggle/Cerebras.
    """
    if sys.platform == "darwin":
        # Block local heavy model loading by default; delegate.
        if os.environ.get("ALLOW_LOCAL_MODEL", "").lower() not in ("1", "true", "yes"):
            raise RuntimeError(
                "Mac=CLI rule: local model.from_pretrained()/heavy compute not allowed on macOS. "
                "Set ALLOW_LOCAL_MODEL=1 to override (not recommended). Delegate to Lightning/Kaggle/Cerebras."
            )

def query_top_hub():
    """
    Pattern: knowledge-rag top-hub (MOC) before planning.
    If knowledge-rag CLI exists, use it; else print guidance.
    """
    kr = shutil.which("knowledge-rag")
    if kr:
        subprocess.run([kr, "top-hub", "--format=json"], check=False)
    else:
        print("[orchestrate] knowledge-rag not found; skipping top-hub query. Install or set PATH.")

def project_mirror_to_parquet(src_dir: str, out_dir: str):
    """
    Project mixed-schema mirror files to {prompt,response} only.
    Writes to batches/mirror-merged/{date}/{slug}.parquet
    """
    import pandas as pd
    from glob import glob

    os.makedirs(out_dir, exist_ok=True)
    files = glob(os.path.join(src_dir, "**/*.jsonl"), recursive=True)
    files += glob(os.path.join(src_dir, "**/*.json"), recursive=True)

    for f in files:
        records = []
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Keep only prompt/response; drop source/ts/extra
                records.append({
                    "prompt": obj.get("prompt") or obj.get("input") or "",
                    "response": obj.get("response") or obj.get("output") or ""
                })
       
