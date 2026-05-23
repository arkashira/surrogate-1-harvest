"""Codebase scanner — full review before each task iteration.

Purpose (per Ashira): full scan first, then grep context that previous iteration
left behind. "Review agent" relies on this to know what was done vs what remains.

3-pass strategy:
  Pass 1: List recently-modified files across watched roots (last 7 days)
  Pass 2: Semantic search via ChromaDB (if index exists) using task keywords
  Pass 3: Git status + diff for any repos found (to detect uncommitted work)

Input: task description (string)
Output: structured summary dict the dispatcher can feed to models as context
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

HOME = Path.home()
WATCHED_ROOTS = [
    HOME / "develope",
    HOME / "axentx",
    HOME / ".surrogate" / "bin",
]
RECENT_DAYS = 7
MAX_FILE_SIZE = 100_000   # skip large binaries
MAX_FILES_PASS1 = 50
MAX_CHUNKS_PASS2 = 10
CHROMA_DB = HOME / ".surrogate" / "code-vector-db"


def _keywords(task: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", task.lower())
    stop = {"a", "an", "the", "is", "are", "was", "were", "be", "to", "and",
            "or", "but", "if", "then", "else", "for", "with", "of", "in", "on",
            "at", "this", "that", "from", "by", "as", "i", "you", "it", "we",
            "they", "write", "create", "make", "build", "add", "update", "task"}
    return [t for t in tokens if len(t) >= 3 and t not in stop][:10]


def _recent_files(keywords: list[str], roots: list[Path]) -> list[dict]:
    """Find recently modified source files matching keywords."""
    cutoff = dt.datetime.now() - dt.timedelta(days=RECENT_DAYS)
    out = []
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # skip hidden, node_modules, .git, venv
            dirnames[:] = [d for d in dirnames if not d.startswith(".")
                           and d not in {"node_modules", "vendor", "venv", ".venv",
                                         "__pycache__", "dist", "build", "target"}]
            for f in filenames:
                p = Path(dirpath) / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_size > MAX_FILE_SIZE:
                    continue
                mtime = dt.datetime.fromtimestamp(st.st_mtime)
                if mtime < cutoff:
                    continue
                # score by keyword hits in name/path
                path_lower = str(p).lower()
                score = sum(1 for kw in keywords if kw in path_lower)
                # light content match (first 4KB only for perf)
                try:
                    with open(p, "r", errors="replace") as fh:
                        head = fh.read(4096).lower()
                    score += sum(1 for kw in keywords if kw in head) * 2
                except OSError:
                    continue
                if score > 0:
                    out.append({
                        "path": str(p),
                        "mtime": mtime.isoformat(),
                        "score": score,
                        "size": st.st_size,
                    })
    out.sort(key=lambda x: -x["score"])
    return out[:MAX_FILES_PASS1]


def _chromadb_search(keywords: list[str], task: str) -> list[dict]:
    """Query ChromaDB semantic index (if available)."""
    if not CHROMA_DB.exists():
        return []
    try:
        # Use existing helper if present
        helper = HOME / ".surrogate" / "bin" / "code-search.sh"
        if helper.exists():
            proc = subprocess.run(
                [str(helper), "--top", str(MAX_CHUNKS_PASS2), task],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0 and proc.stdout:
                out = []
                for line in proc.stdout.splitlines()[:MAX_CHUNKS_PASS2]:
                    m = re.match(r"(\S+):(\d+)\s+(.*)", line)
                    if m:
                        out.append({
                            "path": m.group(1),
                            "line": int(m.group(2)),
                            "preview": m.group(3)[:200],
                        })
                return out
    except (subprocess.TimeoutExpired, OSError):
        pass
    return []


def _git_uncommitted(roots: list[Path]) -> list[dict]:
    """Detect repos with uncommitted work (partial iterations)."""
    out = []
    # Find up to 3 levels of git repos
    for root in roots:
        if not root.exists():
            continue
        for depth_glob in ["*/.git", "*/*/.git", "*/*/*/.git"]:
            for git_dir in root.glob(depth_glob):
                repo = git_dir.parent
                try:
                    status = subprocess.run(
                        ["git", "-C", str(repo), "status", "--short"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if status.returncode == 0 and status.stdout.strip():
                        out.append({
                            "repo": str(repo),
                            "changes": status.stdout.strip().splitlines()[:20],
                        })
                except (subprocess.TimeoutExpired, OSError):
                    continue
    return out


def scan(task: str, task_artifacts: list[str] | None = None) -> dict:
    """Full codebase review → structured context dict.

    Args:
      task: natural-language task description
      task_artifacts: paths mentioned in task (will be loaded in full)

    Returns:
      {
        "keywords": [...],
        "recent_files": [{path, mtime, score, size}, ...],
        "semantic_hits": [{path, line, preview}, ...],
        "uncommitted_repos": [{repo, changes: [...]}, ...],
        "explicit_artifacts": {path: content, ...},  # loaded in full
      }
    """
    keywords = _keywords(task)
    report = {
        "task_excerpt": task[:200],
        "keywords": keywords,
        "recent_files": _recent_files(keywords, WATCHED_ROOTS),
        "semantic_hits": _chromadb_search(keywords, task),
        "uncommitted_repos": _git_uncommitted(WATCHED_ROOTS),
        "explicit_artifacts": {},
    }
    for a in task_artifacts or []:
        p = Path(a)
        if p.exists() and p.is_file() and p.stat().st_size < MAX_FILE_SIZE:
            try:
                report["explicit_artifacts"][str(p)] = p.read_text(errors="replace")[:10000]
            except OSError:
                pass
    return report


def as_context_prompt(scan_result: dict, max_chars: int = 8000) -> str:
    """Render scan as context for LLM system prompt."""
    lines = [
        "## Codebase context (auto-generated)",
        f"Task keywords: {', '.join(scan_result['keywords'])}",
        "",
    ]
    if scan_result["uncommitted_repos"]:
        lines.append("### Uncommitted work (may indicate previous partial iteration):")
        for r in scan_result["uncommitted_repos"][:5]:
            lines.append(f"  {r['repo']}")
            for c in r["changes"][:8]:
                lines.append(f"    {c}")
        lines.append("")

    if scan_result["recent_files"]:
        lines.append(f"### Recently modified relevant files ({len(scan_result['recent_files'])}):")
        for f in scan_result["recent_files"][:15]:
            lines.append(f"  {f['path']} (score={f['score']}, mtime={f['mtime']})")
        lines.append("")

    if scan_result["semantic_hits"]:
        lines.append("### Semantic search hits:")
        for h in scan_result["semantic_hits"][:8]:
            lines.append(f"  {h['path']}:{h.get('line','?')} — {h['preview'][:120]}")
        lines.append("")

    if scan_result["explicit_artifacts"]:
        lines.append("### Explicit task artifacts (FULL content):")
        for path, content in scan_result["explicit_artifacts"].items():
            lines.append(f"--- {path} ---")
            lines.append(content[:3000])
            lines.append("")

    result = "\n".join(lines)
    return result[:max_chars]


if __name__ == "__main__":
    import sys
    task = " ".join(sys.argv[1:]) or "refactor yolo daemon"
    report = scan(task)
    print(json.dumps(
        {k: v if not isinstance(v, list) else v[:5] for k, v in report.items()},
        indent=2, default=str, ensure_ascii=False
    ))
    print("\n=== AS CONTEXT PROMPT ===\n")
    print(as_context_prompt(report, 3000))
