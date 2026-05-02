# vanguard / discovery

Below is the **single, consolidated implementation** that keeps the strongest, most actionable parts from Candidate 1 (and would incorporate Candidate 2 if provided), resolves ambiguities, and prioritizes correctness and immediate usability.

What this final version keeps and why
- Single-file orchestrator (`discover.py`) with clear CLI and JSON report: maximizes portability and automation.
- Lightweight local repo/doc summary + optional HF dataset file listing (non-recursive): reduces API pressure and enables CDN-only training.
- Explicit knowledge-RAG integration path with graceful fallback: ensures the tool works even when RAG services aren’t available, while encouraging correct usage.
- Defensive, minimal dependencies and UTC timestamps: improves reliability across environments.
- Concrete verification and quickstart steps: makes correctness easy to confirm and next actions immediately executable.

Final file: `/opt/axentx/vanguard/discover.py`
```python
#!/usr/bin/env python3
"""
Discovery orchestrator for vanguard.
- Summarizes recent repo/docs state.
- Queries knowledge-rag for top hub and related docs (with fallback).
- Optionally lists HF dataset files for a date folder (non-recursive) and emits
  file-list.json to enable CDN-only training reads and avoid API rate limits.
- Outputs discovery-report.json with top hub, key docs, and suggested next actions.

Usage:
  ./discover.py [--date YYYY-MM-DD] [--hf-repo <repo>] [--output <path>]
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).parent
REPORT_PATH = REPO_ROOT / "discovery-report.json"
FILE_LIST_PATH = REPO_ROOT / "file-list.json"

# ---------- helpers ----------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def run_cmd(cmd: str, check: bool = True, text: bool = True, **kwargs: Any) -> str:
    """
    Run shell command safely.
    Prefer list form for security when possible; this accepts shell strings for convenience.
    """
    result = subprocess.run(
        cmd, shell=True, check=check, text=text,
        capture_output=True, **kwargs
    )
    return result.stdout.strip()

def safe_run_cmd(cmd: str, default: str = "", **kwargs: Any) -> str:
    try:
        return run_cmd(cmd, **kwargs)
    except subprocess.CalledProcessError:
        return default
    except Exception:
        return default

# ---------- local state summary ----------

def summarize_local_state(repo_root: Path, max_files: int = 20) -> Dict[str, Any]:
    summary = {
        "generated_at": utc_now_iso(),
        "project": "vanguard",
        "focus": "discovery",
        "recent_files": [],
        "top_level": [],
        "notes": []
    }

    # Recent files (mtime within 7 days), exclude common noise
    try:
        find_cmd = (
            f'find "{repo_root}" -maxdepth 5 -type f -mtime -7 '
            f'! -path "*/.git/*" ! -path "*/__pycache__/*" ! -path "*/node_modules/*" '
            f'-printf "%T@ %p\\n" 2>/dev/null | sort -rn | head -{max_files}'
        )
        out = safe_run_cmd(find_cmd)
        if out:
            for line in out.splitlines():
                parts = line.split(" ", 1)
                if len(parts) == 2:
                    summary["recent_files"].append(parts[1])
    except Exception as exc:
        summary["notes"].append(f"Could not enumerate recent files: {exc}")

    # Top-level snapshot
    try:
        top_items = []
        for p in repo_root.iterdir():
            if p.name.startswith("."):
                continue
            if p.is_dir():
                top_items.append(f"{p.name}/")
            elif p.suffix in {".py", ".sh", ".md", ".json", ".yaml", ".yml", ".toml", ".txt"}:
                top_items.append(p.name)
        summary["top_level"] = sorted(top_items)
    except Exception as exc:
        summary["notes"].append(f"Could not list top-level items: {exc}")

    return summary

# ---------- knowledge-rag integration ----------

def query_knowledge_rag(project: str = "vanguard") -> Dict[str, Any]:
    """
    Query knowledge-rag for top hub and related docs.
    Strategy:
    - Try CLI if available (knowledge-rag top-hub --project <project>).
    - If unavailable, return pattern-based fallback with concrete actions.
    """
    # Try direct CLI
    cli_out = safe_run_cmd(f"knowledge-rag top-hub --project {project} 2>/dev/null || true")
    if cli_out:
        return {
            "top_hub": "MOC",
            "raw_output": cli_out,
            "source": "knowledge-rag CLI"
        }

    # Try local script/module fallback paths
    local_candidates = [
        REPO_ROOT / "knowledge-rag" / "cli.py",
        REPO_ROOT / "rag" / "top_hub.py",
        Path("knowledge-rag") / "cli.py",
    ]
    for cand in local_candidates:
        if cand.is_file():
            try:
                local_out = safe_run_cmd(f"python3 {shlex.quote(str(cand))} top-hub --project {project} 2>/dev/null || true")
                if local_out:
                    return {
                        "top_hub": "MOC",
                        "raw_output": local_out,
                        "source": f"local {cand.name}"
                    }
            except Exception:
                continue

    # Fallback: pattern-based guidance (correctness-preserving)
    return {
        "top_hub": "MOC",
        "rationale": "Review the most-connected hub (e.g., MOC) before planning tasks (pattern: top-hub doc insight).",
        "related_docs": [
            "knowledge-rag/graph/hubs/MOC.md",
            "discovery/context.md"
        ],
        "suggested_actions": [
            "Review MOC hub for cross-project dependencies.",
            "Run knowledge-rag locally to refresh hub scores.",
            "Check dataset-mirror ingestion for mixed-schema issues before training."
        ],
        "source": "fallback (pattern-based)"
    }

# ---------- HF dataset file listing (non-recursive) ----------

def list_hf_dataset_files(date_folder: Optional[str], repo: str) -> Dict[str, Any]:
    """
    List HF dataset files non-recursively for a date folder.
    Emits file-list.json for CDN-only training reads to avoid API rate limits.
    """
    try:
        from huggingface_hub import HfApi
    except ImportError:
        return {"note": "huggingface_hub not installed; skipping HF file list."}

    try:
        client = HfApi()
        folder = date_folder or datetime.utcnow().strftime("%Y-%m-%d")
        tree = client.list_repo_tree(repo=repo, path=folder, recursive=False)
        files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]
        file_list = {
            "repo": repo,
            "date_folder": folder,
            "files": files,
            "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main/{folder}/",
            "note": "Embed this list in training scripts; use CDN URLs to bypass API rate limits."
        }
        FILE_LIST_PATH.write_text(json.dumps(file_list, indent=2))
        return file_list
    except Exception as exc:
        return {"error": str(exc), "note": "HF listing skipped (may require auth or repo not exist)."}

# ---------- report assembly ----------

def build_report(summary: Dict[str, Any], rag_result: Dict[str, Any], hf_result: Dict[str, Any]) -> Dict[str, Any]:
    # Normalize rag_result shape
    top_hub = rag_result.get("top_hub") or "MOC"
    related_docs = rag_result.get("related_docs") or []
    if isinstance(related_docs, str):
        related_docs = [related_docs]

    next_actions: List[str] = [
        f"Review top hub ({
