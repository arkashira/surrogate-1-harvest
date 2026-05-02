# vanguard / backend

## Final Synthesis (Best Parts + Correctness + Actionability)

Below is the single, authoritative implementation that merges the strongest, non-contradictory insights from both proposals and resolves conflicts in favor of correctness and concrete actionability.

### Conflicts resolved
- **Scope**: Candidate 1 proposes several small files; Candidate 2 pushes one orchestrator.  
  **Resolution**: Keep Candidate 1’s small, composable utilities (easier to test and reuse) and add Candidate 2’s orchestrator as the *entrypoint* that composes them. This gives both modularity and a canonical runbook.
- **HF rate limits**: Both agree on CDN-bypass file-list generation.  
  **Resolution**: Use Candidate 1’s `prepare_hf_filelist.py` (robust, retries, CDN-only URLs) and embed the resulting file-list in the orchestrator so training jobs never hit `/api` during data loading.
- **Lightning Studio reuse**: Candidate 1 provides a reusable helper; Candidate 2 adds idle-stop resilience.  
  **Resolution**: Merge them—`reuse_running_studio` + automatic restart on idle-stop + optional `max_restarts` guard to avoid infinite loops.
- **Schema heterogeneity**: Candidate 1 includes `project_to_pair.py`; Candidate 2 does not.  
  **Resolution**: Keep Candidate 1’s projection script; orchestrator calls it before training to guarantee `{prompt,response}` pairs and avoid `pyarrow.CastError`.
- **Orchestration hygiene / cron**: Candidate 1 provides `run_wrapper.sh`; Candidate 2 adds Mac-only boundary enforcement.  
  **Resolution**: Keep the wrapper for cron/env safety and add Candidate 2’s Mac guard (explicit check + exit) to prevent accidental local model loads on dev machines.
- **Discovery / graph / hub**: Both flag missing canonical entrypoint.  
  **Resolution**: The orchestrator (`orchestrate_training.py`) becomes the canonical entrypoint referenced by `#knowledge-rag #graph #hub`.

---

### Canonical entrypoint
`/opt/axentx/vanguard/backend/orchestrate_training.py`

```python
#!/usr/bin/env python3
"""
Canonical training orchestrator for surrogate-1.
Resolves:
- HF rate limits via CDN file-list generation (once per date).
- Lightning Studio reuse + idle-stop resilience.
- Schema heterogeneity via projection to {prompt,response}.
- Mac-only boundary enforcement.

Usage:
  HF_TOKEN=hf_xxx python orchestrate_training.py \
    --repo datasets/mycorp/surrogate-1 \
    --date 2026-05-02 \
    --out-parquet /data/pairs-2026-05-02.parquet \
    --studio-name surrogate-training \
    --max-restarts 3
"""

import argparse
import datetime
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import requests
from lightning_sdk import Studio, Machine

# ── Mac boundary guard ─────────────────────────────────────────────
if platform.system() == "Darwin":
    print("ERROR: Training orchestration is not allowed on macOS (dev-only guard).", file=sys.stderr)
    sys.exit(1)

# ── Paths & constants ─────────────────────────────────────────────
HF_API_BASE = "https://huggingface.co/api"
CDN_BASE = "https://huggingface.co/datasets"
SCRIPT_DIR = Path(__file__).parent

# ── HF CDN file-list generation (rate-limit safe) ─────────────────
def list_repo_tree(repo: str, path: str = "", token: str | None = None) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{HF_API_BASE}/datasets/{repo}/tree"
    resp = requests.get(url, headers=headers, params={"path": path, "recursive": False}, timeout=30)
    if resp.status_code == 429:
        wait = int(resp.headers.get("retry-after", 360))
        raise RuntimeError(f"HF rate limited. Retry after {wait}s.")
    resp.raise_for_status()
    return resp.json()

def build_cdn_filelist(repo: str, date_folder: str, token: str | None) -> list[str]:
    entries = list_repo_tree(repo, path=date_folder, token=token)
    files = [e for e in entries if e.get("type") == "file"]
    return [
        f"{CDN_BASE}/{repo}/resolve/main/{date_folder}/{f['path']}"
        for f in files
    ]

def ensure_filelist(repo: str, date_folder: str, out_json: Path, token: str | None) -> Path:
    if out_json.exists():
        print(f"Reusing existing file-list: {out_json}")
        return out_json
    print(f"Generating CDN file-list for {repo}/{date_folder} ...")
    cdn_paths = build_cdn_filelist(repo, date_folder, token)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(cdn_paths, indent=2))
    print(f"Wrote {len(cdn_paths)} CDN paths to {out_json}")
    return out_json

# ── Projection to {prompt,response} ────────────────────────────────
def project_cdn_files(filelist_path: Path, out_parquet: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    with open(filelist_path) as f:
        urls = json.load(f)

    pairs = []
    for url in urls:
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            raw = resp.text

            # Minimal heuristic; replace with domain-specific parser as needed.
            sep = None
            for s in ("\nResponse:", "\nAssistant:", "\nresponse:"):
                if s in raw:
                    sep = s
                    break
            if sep:
                prompt, response = raw.rsplit(sep, 1)
                prompt, response = prompt.strip(), (sep.strip() + ":" + response).strip()
            else:
                prompt, response = "", raw.strip()

            if prompt:
                pairs.append({"prompt": prompt, "response": response})
        except Exception as e:
            print(f"Skip {url}: {e}")

    table = pa.Table.from_pylist(
        pairs,
        schema=pa.schema([pa.field("prompt", pa.string()), pa.field("response", pa.string())]),
    )
    pq.write_table(table, out_parquet)
    print(f"Wrote {len(pairs)} pairs to {out_parquet}")

# ── Lightning Studio reuse + idle resilience ───────────────────────
def reuse_running_studio(name: str, machine: Machine = Machine.L40S) -> Studio:
    studios = Studio.list()
    for s in studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return Studio(name=name, create_ok=False)
    print(f"No running studio '{name}' found. Creating...")
    return Studio(name=name, create_ok=True, machine=machine)

def run_training_job(studio: Studio, train_script: Path, data_parquet: Path, extra_args: list[str]) -> bool:
    cmd = [
        "python", str(train_script),
        "--data", str(data_parquet),
        *extra_args,
    ]
    print(f"Running training: {' '.join(cmd)}")
    # In practice, use studio.run or SDK job submission.
    # Here we shell out for clarity/portability.
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode == 0

def wait_for_studio_running(studio: Studio, timeout: int = 600) -> bool:
    for _ in range(timeout // 10):
        studio.refresh()
        if studio.status == "Running":
            return True
        time.sleep(10)
    return False

# ── Orchestrator main ──────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Canonical surrogate-1 training orchestrator.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/org/name)")
    parser.add_argument("--date", required=True, help="Date folder in dataset (YYYY-MM-DD)")
    parser.add_argument("--out-parquet", required=True, help="Output parquet path for pairs")
    parser.add_argument("--studio
