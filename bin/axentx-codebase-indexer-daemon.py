#!/usr/bin/env python3
"""axentx codebase-indexer — for each project repo, reads README + key
source files + tells LLM 'what does this project ACTUALLY do', writes
the TRUTH to shared_knowledge["project-truth/<slug>"].

User feedback 2026-05-04:
  > 'workio --> gh-codescan เกี่ยวอะไร มันเป็น platform สำหรับ ลงเวลาทำงาน
  >  กับ payroll เหี่ยวไรกับ gh-codescan จ่าย feature ได้ไง ... ดู codebase
  >  ของทุก project ที่มีอยู่ เอาเข้า context เอาเข้า knowledge เอาเข้า
  >  memory ไว้ก่อน ให้รู้ว่าแต่ละ project ทำอะไรจริงๆ กันแน่'

Why: portfolio descriptions in shared_kv["bd.portfolio"] are auto-extracted
from /business/business-model-canvas.md "Value Propositions" block — these
are STALE / LLM-synthesized / wrong (e.g. workio described as "Zapier for
eng teams" when actual code is LINE punch-in/payroll for Thai SMEs).

Truth source:
  1. README.md (top of repo — usually most accurate)
  2. package.json / pyproject.toml / Cargo.toml (lists deps → tells stack)
  3. Key src file (main.go / app.ts / index.py / src/main.* / app.py)
  4. docker-compose.yml (services list → reveals architecture)

LLM combines these → 1-paragraph "what is this" + 1-line summary +
tech-stack tags + audience-guess (if README hints at it).

Cycle: every 1h (truths don't change rapidly). Leader=GCP (single-host
write to canonical key). bd / portfolio-syncer / feature-synth READ
project-truth from shared_knowledge — never the stale BMC excerpt.
"""
from __future__ import annotations
import datetime
import json
import os
import re
import signal
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop, call_llm  # noqa: E402

POLL_SEC = int(os.environ.get("CODEBASE_INDEXER_POLL_SEC", "3600"))
HOST = socket.gethostname()
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _read(p: Path, n: int = 2000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:n]
    except Exception:
        return ""


# Files that reveal what a project IS (in priority order).
# We sample up to MAX_TRUTH_BYTES total per project.
TRUTH_FILES = [
    "README.md", "readme.md",
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "docker-compose.yml", "Dockerfile",
    "main.go", "main.py", "main.ts", "main.rs",
    "app.py", "app.ts", "app.go", "index.ts", "index.js",
    "src/main.py", "src/main.ts", "src/main.rs", "src/main.go",
    "src/app.ts", "src/app.py", "src/index.ts",
    "internal/config", "schemas/",   # llama-gate-shaped repos
]
MAX_TRUTH_BYTES = 6000


def gather_repo_truth(repo: Path) -> str:
    """Returns concatenated content from key files, capped at MAX_TRUTH_BYTES."""
    parts: list[str] = []
    used = 0
    for rel in TRUTH_FILES:
        p = repo / rel
        if p.is_file():
            txt = _read(p, 2000)
            if not txt:
                continue
            block = f"\n\n## {rel}\n```\n{txt}\n```"
            if used + len(block) > MAX_TRUTH_BYTES:
                break
            parts.append(block)
            used += len(block)
        elif p.is_dir():
            # Show top files in directory
            try:
                items = sorted([x.name for x in p.iterdir()
                               if x.is_file() and not x.name.startswith(".")])[:8]
                if items:
                    parts.append(f"\n## {rel}/ contents\n  - " + "\n  - ".join(items))
            except Exception:
                pass
    # Always include directory listing of repo root
    try:
        root_items = sorted([x.name for x in repo.iterdir()
                            if not x.name.startswith(".")])[:25]
        parts.insert(0, f"\n## Top-level files/dirs\n  " + ", ".join(root_items))
    except Exception:
        pass
    return "\n".join(parts)[:MAX_TRUTH_BYTES]


TRUTH_SYSTEM = (
    "You inspect a project's actual codebase + README and produce the "
    "GROUND TRUTH about what the project really does. Output STRICT JSON:\n"
    "{\n"
    '  "one_liner": "1-sentence: what does this project do, who uses it?",\n'
    '  "what_it_actually_is": "1 paragraph (max 5 sentences) — '
    'concrete description of functionality based on visible code",\n'
    '  "tech_stack": ["framework1","language2","db3"],\n'
    '  "audience_actual": "the actual user persona implied by code/README '
    '(may differ from any marketing description)",\n'
    '  "language_of_repo": "en|th|mixed — based on README/comments",\n'
    '  "category": "best 1-2 word category (e.g. payroll, llm-gateway, '
    'workflow, finops, security, observability, ...)",\n'
    '  "stage": "skeleton|early|active|mature — how built-out is it?"\n'
    "}\n"
    "Be PRECISE. If README says 'Workio is a LINE punch-in payroll system', "
    "category MUST be 'payroll' — not 'workflow'. If audience is Thai SMEs, "
    "say so. Don't invent or upgrade — describe what's literally there.")


def index_one(slug: str, repo: Path) -> dict | None:
    truth_blob = gather_repo_truth(repo)
    if len(truth_blob) < 200:
        log("codebase-indexer", f"  ⊘ {slug}: too thin to index")
        return None
    prompt = (f"# Project: {slug}\n# Repo path: {repo}\n\n"
              f"{truth_blob}\n\nGround-truth STRICT JSON only.")
    try:
        out = call_llm(prompt, system=TRUTH_SYSTEM,
                       max_tokens=600, timeout=30)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        v = json.loads(txt.strip())
        if not isinstance(v, dict):
            return None
        v["_indexed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
        v["_indexed_by"] = HOST
        v["_repo_path"] = str(repo)
        return v
    except Exception as e:
        log("codebase-indexer",
            f"  ✗ {slug}: {type(e).__name__}: {str(e)[:80]}")
        return None


def write_truth(slug: str, truth: dict) -> bool:
    """shared_knowledge entry for project-truth/<slug>."""
    if not (SB_URL and SB_KEY):
        return False
    try:
        body = json.dumps(truth, ensure_ascii=False)[:30000]
        payload = json.dumps({
            "p_slug": f"project-truth/{slug}",
            "p_category": "project-truth",
            "p_title": (f"{slug} (truth): {truth.get('one_liner', '')[:120]}"),
            "p_body": body,
            "p_metadata": {
                "category": truth.get("category"),
                "stage": truth.get("stage"),
                "language_of_repo": truth.get("language_of_repo"),
            },
            "p_who": "codebase-indexer",
        }).encode()
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/rpc/shared_knowledge_set",
            data=payload, method="POST",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        log("codebase-indexer", f"  ✗ write {slug}: {e}")
        return False


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("codebase-indexer", "  ⤷ not leader — skip")
        return False
    if not PROJECTS_ROOT.exists():
        log("codebase-indexer", f"  ⊘ {PROJECTS_ROOT} missing — skip")
        return False

    repos = []
    for r in sorted(PROJECTS_ROOT.iterdir()):
        if r.is_dir() and (r / ".git").exists():
            repos.append((r.name, r))
    if not repos:
        log("codebase-indexer", "  ⊘ no project repos — skip")
        return False

    log("codebase-indexer", f"▸ indexing {len(repos)} projects")
    indexed = 0
    truths_summary: dict[str, dict] = {}
    for slug, repo in repos:
        truth = index_one(slug, repo)
        if not truth:
            continue
        if write_truth(slug, truth):
            indexed += 1
            truths_summary[slug] = {
                "one_liner": truth.get("one_liner", "")[:200],
                "category": truth.get("category"),
                "audience": truth.get("audience_actual", "")[:120],
                "stage": truth.get("stage"),
                "lang": truth.get("language_of_repo"),
            }
            log("codebase-indexer",
                f"  ✓ {slug} [{truth.get('category', '?')}/"
                f"{truth.get('stage', '?')}]: "
                f"{truth.get('one_liner', '')[:80]}")

    # Also write a roll-up so other agents can read all truths in one query
    try:
        from axentx_shared import kv_set
        kv_set("project-truth.all", {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "host": HOST,
            "n": len(truths_summary),
            "projects": truths_summary,
        })
    except Exception:
        pass

    log("codebase-indexer",
        f"  ✓ indexed {indexed}/{len(repos)} project truths "
        f"to shared_knowledge[project-truth/*]")
    return False


if __name__ == "__main__":
    daemon_loop("codebase-indexer", POLL_SEC, cycle)
