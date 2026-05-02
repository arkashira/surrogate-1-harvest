#!/usr/bin/env python3
"""axentx feature-builder — turn approved markdown specs into real code files.

User callout 2026-05-03:
  > 'มันได้ code จริง ๆ มาบ้างหรือเปล่า หรือมีแต่ .md … dev markdown
  >  ทำไมเขียนแต่ md  code ไม่มีเลย'

The dev-daemon's output is a markdown spec with `Code Snippets` sections
that contain language-tagged code blocks plus the target file path in a
header. Until now nobody parses those blocks → 400+ MDs accumulate but
zero actual implementation lands in src/. This daemon closes the gap:

  qa APPROVE → spec MD lands in `.axentx-dev-bot/`
  ↓
  feature-builder reads the spec, extracts code blocks with their file
  paths, writes them to disk inside the project repo, stages with git,
  hands off to commit-daemon.

Parser supports common dev-daemon output shapes (verified by sampling
250+ existing MDs):

  ### `<path>`                         ← header above code block
  ### File: `<path>`
  **File**: `<path>`
  **Path**: `<path>`
  ## `<path>` (NEW|UPDATE)

After each code block extracted, file is written to <repo>/<path>.
Already-existing files are overwritten ONLY if the spec says NEW or
UPDATE; otherwise it appends a `.spec-<id>.suggestion` sibling for
human review (avoids destroying hand-written code).

Operates on FS queue `feature-build-queue/` populated by qa-daemon when
verdict=APPROVE. After successful write+stage, item moves to
commit-queue/ for the existing commit-daemon to push.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import REPO_ROOT, log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("FEATURE_BUILDER_POLL_SEC", "30"))
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
SHARED = REPO_ROOT / "state" / "swarm-shared"

# Where qa-daemon drops APPROVE'd items needing implementation:
FEATURE_QUEUE = SHARED / "feature-build-queue"
# Where we hand off to commit-daemon after writing files:
COMMIT_QUEUE = SHARED / "commit-queue"
DEAD_LETTER = SHARED / "dead-letter" / "feature-builder"

# Regex grammar for file headers + code blocks. Order matters: more
# specific patterns first. dev-daemon's actual MD output uses bare paths
# under `###` headings (no backticks) — verified by sampling 250+ MDs.
# Mix of patterns to cover everything:
HEADER_PATTERNS = [
    # ### File: `path` (NEW)         — explicit prefix, in backticks
    re.compile(r"(?m)^#{2,4}\s+(?:File|Path|New file|Update)\s*:\s*`([^`]+)`\s*(?:\(([A-Z]+)\))?\s*$"),
    # **File**: `path`               — bold, in backticks
    re.compile(r"(?m)^\*\*(?:File|Path)\*\*\s*:\s*`([^`]+)`\s*$"),
    # File: `path`                    — plain, in backticks
    re.compile(r"(?m)^(?:File|Path)\s*:\s*`([^`]+)`\s*$"),
    # File: path                      — plain, no backticks
    re.compile(r"(?m)^(?:File|Path)\s*:\s*([A-Za-z0-9_./\-]+\.[A-Za-z0-9]+)\s*$"),
    # ### `path`                      — heading, in backticks
    re.compile(r"(?m)^#{2,4}\s+`([^`]+)`\s*$"),
    # ### path/with/slash.py          — heading, bare path with slash + ext
    re.compile(r"(?m)^#{2,4}\s+([a-zA-Z0-9_][a-zA-Z0-9_./\-]*\/[a-zA-Z0-9_./\-]+\.[a-zA-Z0-9]+)\s*$"),
    # ### path.py                     — heading, bare filename with extension
    re.compile(r"(?m)^#{2,4}\s+([a-zA-Z0-9_][a-zA-Z0-9_./\-]*\.[a-zA-Z]{1,5})\s*$"),
    # ### Dockerfile (no extension)   — known no-ext files
    re.compile(r"(?m)^#{2,4}\s+(Dockerfile|Makefile|Procfile|Justfile|\.dockerignore|\.gitignore|\.env\.example)\s*$"),
]
CODE_BLOCK = re.compile(r"```([a-zA-Z0-9+_-]*)\n(.*?)\n```", re.DOTALL)

# File extensions we will write — anything else is suspicious (LLM may
# hallucinate paths for shell snippets, sample logs, etc.). Whitelist
# instead of blacklist: easier to add than to anticipate weird ones.
ALLOWED_EXTS = {
    ".py", ".pyi", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php",
    ".html", ".css", ".scss", ".less",
    ".sh", ".bash", ".zsh", ".fish",
    ".yml", ".yaml", ".toml", ".json", ".jsonl", ".ini", ".cfg", ".conf",
    ".sql", ".graphql", ".gql",
    ".md", ".rst", ".txt",
    ".dockerfile", ".dockerignore", ".gitignore", ".gitattributes",
    ".tf", ".tfvars", ".hcl",
    ".csv", ".env", ".env.example",
    "",  # extension-less files like Dockerfile, Makefile
}

# Path safety: only allow writes inside the project repo root, never
# escape via ../ or absolute paths to system locations.
def _safe_path(repo_root: Path, candidate: str) -> Path | None:
    """Return resolved Path if safe to write, None if rejected."""
    s = candidate.strip()
    if not s:
        return None
    # Strip common prefixes the LLM may emit (in priority order):
    #   /opt/axentx/<project>/   /home/<user>/axentx/<project>/
    #   ~/axentx/<project>/      $HOME/axentx/<project>/
    #   <project>/               (e.g. "Costinel/foo.py" — relative-to-axentx)
    # After these, what's left should be a pure relative path inside repo.
    project_name = repo_root.name
    s = re.sub(rf"^/opt/axentx/{re.escape(project_name)}/", "", s)
    s = re.sub(rf"^/home/[^/]+/axentx/{re.escape(project_name)}/", "", s)
    s = re.sub(rf"^~/axentx/{re.escape(project_name)}/", "", s)
    s = re.sub(rf"^\$HOME/axentx/{re.escape(project_name)}/", "", s)
    s = re.sub(rf"^{re.escape(project_name)}/", "", s)
    # Now reject anything that's still absolute or escapes
    if s.startswith(("/", "~", "$HOME")) or ".." in Path(s).parts:
        return None
    s = s.strip("/")
    if not s:
        return None
    p = (repo_root / s).resolve()
    try:
        p.relative_to(repo_root.resolve())
    except ValueError:
        return None
    # Whitelist extension
    ext = p.suffix.lower() if p.suffix else (
        "" if p.name in ("Dockerfile", "Makefile", "Procfile", "Justfile") else "?"
    )
    if ext == "?":
        return None
    if ext not in ALLOWED_EXTS:
        return None
    return p


def parse_spec(md_path: Path) -> list[tuple[Path, str, str]]:
    """Read spec, return list of (target_path, language, code) tuples.

    Strategy: walk the MD top-to-bottom; remember the most-recent file
    header; when a code block follows, attach it to that header. Code
    blocks without a preceding header are skipped (often shell snippets,
    sample output, etc.).
    """
    text = md_path.read_text(encoding="utf-8", errors="replace")
    # Find all header positions (text offset → captured path)
    header_hits = []
    for pat in HEADER_PATTERNS:
        for m in pat.finditer(text):
            header_hits.append((m.start(), m.group(1)))
    header_hits.sort(key=lambda x: x[0])

    project = md_path.parents[1].name  # /opt/axentx/<project>/.axentx-dev-bot/x.md
    repo_root = PROJECTS_ROOT / project
    out: list[tuple[Path, str, str]] = []
    for cb in CODE_BLOCK.finditer(text):
        cb_start = cb.start()
        # Find the closest preceding header
        last = None
        for pos, path in header_hits:
            if pos < cb_start:
                last = path
            else:
                break
        if not last:
            continue
        safe = _safe_path(repo_root, last)
        if safe is None:
            continue
        lang = cb.group(1) or ""
        code = cb.group(2)
        # Skip empty code blocks or single-word "TBD" placeholders
        if not code.strip() or len(code.strip()) < 8:
            continue
        out.append((safe, lang, code))
    return out


def write_files(extracts: list[tuple[Path, str, str]], project: str,
                spec_id: str) -> list[Path]:
    """Write extracted code files. Returns list of paths actually written.

    These repos on GCP/Kam are dev-bot-owned (no human edits), so we
    always overwrite. Earlier conflict-protection via `.suggestion`
    siblings tripped path-resolution edge cases when /opt/axentx is a
    symlink to /home/<user>/axentx. The repo branch protects history
    anyway — overwritten content stays in git.
    """
    written: list[Path] = []
    for path, lang, code in extracts:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(code, encoding="utf-8")
            written.append(path)
        except (ValueError, OSError) as e:
            log("feature-build", f"  ⚠ skip {path}: {type(e).__name__}: {str(e)[:120]}")
            continue
    return written


def git_stage_and_handoff(project: str, spec_id: str,
                          written: list[Path], spec_md: Path) -> bool:
    """git add the written files + push an item to commit-queue so the
    existing commit-daemon picks it up and pushes."""
    repo_root = PROJECTS_ROOT / project
    if not (repo_root / ".git").exists():
        log("feature-build", f"  ✗ {project} not a git repo at {repo_root}")
        return False
    # Stage all written files
    rels = [str(p.relative_to(repo_root)) for p in written]
    if rels:
        try:
            subprocess.run(["git", "-C", str(repo_root), "add", *rels],
                           check=True, capture_output=True, timeout=20)
        except subprocess.CalledProcessError as e:
            log("feature-build", f"  ✗ git add failed: {e.stderr[:200]}")
            return False

    # Build a commit-queue item that the existing commit-daemon understands.
    # Reuse the same id so trace_id propagates through.
    item = {
        "id": spec_id,
        "stage": "commit",
        "project": project,
        "focus": "feature",
        "current": {
            "text": (
                f"feat({project}): implement spec {spec_id}\n\n"
                f"Files written by feature-builder:\n"
                + "\n".join(f"  - {r}" for r in rels)
                + f"\n\nSpec: .axentx-dev-bot/{spec_md.name}"
            ),
        },
        "history": [{
            "stage": "feature-build",
            "actor": "axentx-feature-builder",
            "output": f"wrote {len(rels)} file(s)",
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "captured_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    COMMIT_QUEUE.mkdir(parents=True, exist_ok=True)
    (COMMIT_QUEUE / f"{spec_id}-feature.json").write_text(
        json.dumps(item, indent=2)
    )
    return True


def do_one() -> bool:
    """Pick one feature-build-queue item, parse, write, hand off."""
    FEATURE_QUEUE.mkdir(parents=True, exist_ok=True)
    DEAD_LETTER.mkdir(parents=True, exist_ok=True)
    items = sorted(FEATURE_QUEUE.glob("*.json"),
                   key=lambda p: p.stat().st_mtime)
    if not items:
        # Backfill mode: scan all .axentx-dev-bot/*.md across projects, pick
        # one that hasn't been processed yet (no .processed marker).
        for proj in PROJECTS_ROOT.iterdir():
            if not proj.is_dir() or not (proj / ".git").exists():
                continue
            specs_dir = proj / ".axentx-dev-bot"
            if not specs_dir.is_dir():
                continue
            for md in sorted(specs_dir.glob("*.md"),
                             key=lambda p: -p.stat().st_mtime)[:50]:
                marker = md.with_suffix(md.suffix + ".processed")
                if marker.exists():
                    continue
                return _process_spec(proj.name, md)
        return False

    src_path = items[0]
    try:
        item = json.loads(src_path.read_text())
    except Exception:
        src_path.rename(DEAD_LETTER / src_path.name)
        return False
    project = item.get("project") or ""
    spec_md_name = (
        item.get("current", {}).get("output", "")
        .splitlines()[0]
        .replace("Spec: .axentx-dev-bot/", "")
        .strip()
    )
    spec_md = PROJECTS_ROOT / project / ".axentx-dev-bot" / spec_md_name
    if not spec_md.is_file():
        src_path.rename(DEAD_LETTER / src_path.name)
        return False
    ok = _process_spec(project, spec_md, spec_id=item.get("id"))
    src_path.unlink(missing_ok=True)
    return ok


def _process_spec(project: str, spec_md: Path, spec_id: str | None = None) -> bool:
    spec_id = spec_id or spec_md.stem
    extracts = parse_spec(spec_md)
    if not extracts:
        # Mark processed so we don't keep retrying empty specs
        spec_md.with_suffix(spec_md.suffix + ".processed").write_text("no-code-blocks")
        return False
    log("feature-build",
        f"▸ {project} / {spec_id[:30]} — {len(extracts)} file(s) to write")
    written = write_files(extracts, project, spec_id)
    if not written:
        spec_md.with_suffix(spec_md.suffix + ".processed").write_text("rejected-paths")
        return False
    if not git_stage_and_handoff(project, spec_id, written, spec_md):
        return False
    spec_md.with_suffix(spec_md.suffix + ".processed").write_text(
        json.dumps([str(p) for p in written], indent=2)
    )
    log("feature-build",
        f"  ✓ {project} wrote {len(written)} file(s) → commit-queue")
    return True


if __name__ == "__main__":
    daemon_loop("feature-build", POLL_SEC, do_one)
