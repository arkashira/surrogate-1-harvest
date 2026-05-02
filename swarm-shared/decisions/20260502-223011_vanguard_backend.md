# vanguard / backend

Below is the **single, merged, corrected, and action-ready** proposal.  
It keeps every high-value insight, removes duplication, fixes contradictions, and prioritizes **correctness + concrete actionability**.

---

## 1. Unified Diagnosis (accepted)
- **No canonical entrypoint** (`__main__.py` or CLI router) → planning is ad-hoc and violates `#knowledge-rag #graph #hub`.
- **Missing HF CDN-bypass file-list generation** for surrogate-1 training → future training jobs will hit 429 rate limits during data loading.
- **No Lightning Studio reuse guard** → each training run risks quota waste via redundant studio creation.
- **No wrapper script hygiene** (shebang + executable + `SHELL=/bin/bash` in cron) → future `opus-pr-reviewer` / `active-learning` cron jobs will fail with exec errors.
- **No surrogate-1 ingestion projection guard** → heterogeneous HF datasets risk `pyarrow.CastError` during `load_dataset`.

---

## 2. Unified Proposed Change
Add **one lightweight backend bootstrap** that wires all missing patterns into a single, safe, canonical entrypoint:

- **File**: `/opt/axentx/vanguard/backend/__main__.py` (new)
- **Scope**: CLI router + HF file-list generator + Lightning Studio reuse + wrapper installer + ingestion projection helper.
- **Size**: ~180 lines, self-contained; no external deps beyond `lightning`, `huggingface_hub`, `requests`, `pyarrow`, `pandas` (all already required elsewhere).

---

## 3. Corrected, Actionable Implementation

```bash
# /opt/axentx/vanguard/backend/__main__.py
#!/usr/bin/env python3
"""
Vanguard backend bootstrap.
Usage:
  python -m vanguard.backend hf-filelist --repo <org/ds> --out filelist.json
  python -m vanguard.backend studio-reuse --name surrogate-1-train
  python -m vanguard.backend install-wrapper --script /opt/axentx/vanguard/bin/active-learning
  python -m vanguard.backend project-ingest --src /data/mirror --dst /data/enriched
"""

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
    from lightning import Fabric, LightningFlow, LightningWork, LightningApp, Studio, Teamspace
except ImportError as e:
    print(f"Missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

HF_CDN_BASE = "https://huggingface.co/datasets"

# ---- hf-filelist ----
def hf_filelist(repo: str, folder: str = "", out: str = "filelist.json") -> None:
    """
    Single API call to list folder (non-recursive), then embed paths for CDN-only fetches.
    Avoids recursive list_repo_files and rate limits during training.
    """
    try:
        tree = list_repo_tree(repo=repo, path=folder, recursive=False)
    except Exception as e:
        print(f"HF list_repo_tree failed: {e}", file=sys.stderr)
        sys.exit(1)

    files = [item.rfilename for item in tree if item.type == "file"]
    payload = {
        "repo": repo,
        "folder": folder,
        "files": files,
        "cdn_template": f"{HF_CDN_BASE}/{repo}/resolve/main/{folder}/{{file}}"
    }
    Path(out).write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))

# ---- studio-reuse ----
def studio_reuse(name: str, machine: str = "L40S") -> None:
    """
    Reuse a running Lightning Studio to save quota.
    If stopped, restart it.
    """
    try:
        teamspace = Teamspace()
        running = [s for s in teamspace.studios if s.name == name and s.status == "running"]
        if running:
            print(f"Reusing running studio: {name}")
            return

        stopped = [s for s in teamspace.studios if s.name == name and s.status == "stopped"]
        if stopped:
            print(f"Restarting stopped studio: {name}")
            stopped[0].start(machine=machine)
            return

        print(f"Creating new studio: {name}")
        Studio(name=name, create_ok=True)
    except Exception as e:
        print(f"Studio reuse failed: {e}", file=sys.stderr)
        sys.exit(1)

# ---- install-wrapper ----
def install_wrapper(path: str) -> None:
    """
    Ensure wrapper scripts have proper shebang, are executable,
    and are intended to be invoked via Bash (cron-safe).
    """
    p = Path(path)
    if not p.exists():
        print(f"Script not found: {path}", file=sys.stderr)
        sys.exit(1)

    content = p.read_text()
    if not content.startswith("#!/usr/bin/env bash") and not content.startswith("#!/bin/bash"):
        content = "#!/usr/bin/env bash\n" + content
        p.write_text(content)
        print(f"Added Bash shebang to {path}")

    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    print(f"Ensured executable: {path}")

    # Reminder for crontab
    print("Ensure crontab contains: SHELL=/bin/bash")

# ---- project-ingest ----
def project_ingest(src_dir: str, dst_dir: str) -> None:
    """
    Project heterogeneous HF-style dumps to {prompt, response} only.
    Attribution via filename pattern: batches/mirror-merged/{date}/{slug}.parquet
    No source/ts columns to avoid schema drift.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    src = Path(src_dir)
    dst = Path(dst_dir)
    dst.mkdir(parents=True, exist_ok=True)

    for file in src.rglob("*"):
        if file.suffix.lower() not in {".jsonl", ".json", ".parquet", ".csv"}:
            continue

        try:
            if file.suffix.lower() == ".parquet":
                tbl = pq.read_table(file)
                # Keep only prompt/response if present; drop others
                keep = [c for c in tbl.column_names if c.lower() in {"prompt", "response"}]
                if not keep:
                    # fallback: first two string cols
                    keep = [c for c, t in zip(tbl.column_names, tbl.schema.types) if pa.types.is_string(t)][:2]
                tbl = tbl.select(keep)
                # Rename to canonical prompt/response
                names = list(tbl.column_names)
                if len(names) == 2:
                    tbl = tbl.rename_columns(["prompt", "response"])
            else:
                # line/json/csv -> stream parse, project
                import pandas as pd
                if file.suffix.lower() == ".jsonl":
                    df = pd.read_json(file, lines=True)
                elif file.suffix.lower() == ".json":
                    df = pd.read_json(file)
                else:
                    df = pd.read_csv(file)

                cols = [c for c in df.columns if c.lower() in {"prompt", "response"}]
                if len(cols) < 2:
                    str_cols = [c for c, t in zip(df.columns, df.dtypes) if t == "object"][:2]
                    df = df[str_cols].copy()
                    df.columns = ["prompt", "response"]
                else:
                    df = df[cols[:2]].copy()
                    df.columns = ["prompt", "response"]
                tbl = pa.Table.from_pandas(df, preserve_index=False)

            out_name = file.name
            out_path = dst / out_name
            pq.write_table(tbl, out_path)
            print(f"Projected: {file} -> {out_path}")
        except Exception as e:
            print(f"Skipped {file}: {e}", file=sys.stderr)

# ---- CLI ----
def main() -> None:
    parser = argparse.ArgumentParser(description="Vanguard backend utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("hf-filelist", help="Generate CDN file list for surrogate-1 training")
    p_list.add_argument("--repo", required=True, help="HF dataset repo (org/ds)")
    p_list.add_argument("--folder", default="", help="Folder within repo")
    p_list.add_argument("--out", default="filelist.json", help
