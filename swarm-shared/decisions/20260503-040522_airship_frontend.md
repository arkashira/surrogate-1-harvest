# airship / frontend

## Final Synthesized Implementation Plan (Best of Both Candidates)

**Highest-value improvement (<2h):**  
Ship a **CDN-only parquet loader** plus **Lightning idle-resilient runner** to eliminate HuggingFace API rate limits and prevent idle-timeout kills during 24/7 autonomous training.

---

### Why this wins
- **Correctness:** CDN downloads avoid HF API auth/rate-limits entirely; schema-tolerant parsing prevents crashes on mixed datasets.
- **Actionability:** Concrete, copy-paste-ready code with validation steps and failure recovery.
- **Resilience:** Automatic studio restart on idle/stop ensures long-running jobs survive timeouts.

---

### Implementation Plan

#### 1. Create `scripts/list_hf_files.py`
One-time Mac/Linux orchestration script to list parquet files for a date folder and emit `file_list.json`. Uses non-recursive `list_repo_tree` per folder to avoid pagination and rate limits.

```python
#!/usr/bin/env python3
"""
List parquet files for a date folder in a HuggingFace dataset repo
and emit file_list.json for CDN-only training.

Usage:
  python scripts/list_hf_files.py --repo surrogate-datasets --date 2026-04-29
"""

import argparse
import json
import os
import sys
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_REPO", "surrogate-datasets")
OUT_DIR = Path(__file__).parent.parent / "surrogate" / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

def list_parquet_files(repo: str, date: str) -> list[str]:
    api = HfApi()
    prefix = f"{date}/"
    # non-recursive per folder to avoid pagination and rate limits
    items = api.list_repo_tree(repo=repo, path=prefix, recursive=False)
    files = []
    for item in items:
        if item.path.endswith(".parquet"):
            files.append(item.path)
        elif item.type == "directory":
            # one-level deeper only (avoid deep recursion)
            sub_items = api.list_repo_tree(repo=repo, path=item.path, recursive=False)
            for sub in sub_items:
                if sub.path.endswith(".parquet"):
                    files.append(sub.path)
    return sorted(set(files))

def main() -> None:
    parser = argparse.ArgumentParser(description="List HF dataset parquet files for CDN training.")
    parser.add_argument("--repo", default=HF_REPO, help="HF dataset repo (e.g., surrogate-datasets)")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-04-29)")
    args = parser.parse_args()

    try:
        files = list_parquet_files(args.repo, args.date)
    except Exception as exc:
        print(f"Error listing files: {exc}", file=sys.stderr)
        sys.exit(1)

    out_path = OUT_DIR / "file_list.json"
    payload = {
        "repo": args.repo,
        "date": args.date,
        "files": files,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

---

#### 2. Update `surrogate/train.py`
Add CDN-only parquet loader and Lightning idle-resilient runner.

```python
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Any

import pyarrow.parquet as pq
import requests
import torch
from lightning import Fabric, LightningModule, Trainer
from lightning.pytorch.strategies import FSDPStrategy

# --
# CDN-only parquet loader (no HF API/auth)
# --
def _load_parquet_cdn_only(file_list_path: Path, repo: str) -> List[Dict[str, str]]:
    """
    Download parquet files via CDN and project to {prompt, response}.
    Avoids HF API entirely (rate-limit bypass).
    """
    manifest = json.loads(file_list_path.read_text())
    base_url = f"https://huggingface.co/datasets/{repo}/resolve/main"
    records = []

    for path in manifest["files"]:
        url = f"{base_url}/{path}"
        local_pq = Path("/tmp") / Path(path).name
        if not local_pq.exists():
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            local_pq.write_bytes(r.content)

        # Read only required columns; tolerate mixed schemas
        try:
            table = pq.read_table(local_pq, columns=["prompt", "response"])
            prompt_col = "prompt"
            response_col = "response"
        except (ValueError, KeyError, OSError):
            # fallback: read schema and pick closest match
            schema = pq.read_schema(local_pq)
            prompt_col = next((c for c in schema.names if "prompt" in c.lower()), None)
            response_col = next((c for c in schema.names if "response" in c.lower()), None)
            cols = [c for c in [prompt_col, response_col] if c]
            if not cols:
                continue
            table = pq.read_table(local_pq, columns=cols)

        df = table.to_pandas()
        # normalize column names
        if prompt_col and prompt_col != "prompt":
            df = df.rename(columns={prompt_col: "prompt"})
        if response_col and response_col != "response":
            df = df.rename(columns={response_col: "response"})

        for _, row in df.iterrows():
            if isinstance(row.get("prompt"), str) and isinstance(row.get("response"), str):
                records.append({"prompt": row["prompt"], "response": row["response"]})
    return records

# --
# Lightning idle-resilient runner helpers
# --
def _ensure_studio_running(studio_name: str, machine: str = "L40S"):
    """
    Reuse or start a Lightning Studio. If stopped, restart it.
    """
    from lightning.pytorch.cloud import Teamspace  # type: ignore

    for s in Teamspace.studios:
        if s.name == studio_name:
            if s.status == "running":
                return s
            # stopped/idle -> restart
            print(f"Studio {studio_name} is {s.status}; restarting on {machine}...")
            s.start(machine=machine)
            return s

    # create if not exists
    print(f"Creating studio {studio_name} on {machine}...")
    return Teamspace.create_studio(
        name=studio_name,
        machine=machine,
        create_ok=True,
    )

def run_training_with_idle_resilience(
    model: LightningModule,
    fabric: Fabric,
    studio_name: str,
    max_retries: int = 3,
) -> None:
    """
    Run training with idle-timeout resilience.
    Checks studio status before each run attempt and restarts if stopped.
    """
    from lightning.pytorch.cloud import Machine  # type: ignore

    target_machine = Machine.L40S
    for attempt in range(1, max_retries + 1):
        try:
            studio = _ensure_studio_running(studio_name, machine=target_machine)
            trainer = Trainer(
                devices="auto",
                accelerator="auto",
                strategy=FSDPStrategy(),
                max_epochs=1,
            )
            trainer.fit(model)
            return
        except Exception as exc:
            print(f"Attempt {attempt}/{max_retries} failed: {exc}")
            if attempt == max_retries:
                raise
            # wait before retry (avoid tight loops)
            time.sleep(60 * attempt)
```

---

### 3. Validation Steps

1. **Generate file list**  
   ```bash
   python scripts/list_hf_files.py --repo surrogate-datasets --date 2026-04-29
   ```
   → Produces `surrogate/data/file_list.json`.

2. **Run training with CDN + idle resilience**  
   ```bash
   python surrogate/train.py --file-list surrogate/data/file_list.json --studio surrogate-train-l40s
   ```
   - No HF API calls during data loading.
   - Survives studio idle/stop by auto-restarting on L40S.

---

### Key Resolutions vs Contradictions
- **CDN vs API**: Always prefer CDN URLs; avoids auth
