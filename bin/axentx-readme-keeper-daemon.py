#!/usr/bin/env python3
"""axentx readme-keeper — ensures EVERY project has a proper README that
explains what the project IS, how to run it, tech stack, structure.

User feedback 2026-05-04:
  > 'อันนี้นิเนึง ไม่รู้ใครควบทำ น่าจะเป็น tech-lead นะ ต้องเขียนบอกหน่อย
  >  เป็น project เกี่ยวกับอะไร รันยังไง อะไรพวกนี้ ที่เกี่ยวกับโปรเจ็ค
  >  ของแต่ละตัวเลยนะ'

Cycle (event-driven, 5min tick, leader=GCP):
  1. For each /opt/axentx/<slug>/ repo with .git:
     a. Read README.md (if exists)
     b. Check for required sections:
        - ## Overview (what does this project do?)
        - ## Tech stack (link to decisions/tech-stack.md)
        - ## Project structure (top-level dirs)
        - ## Getting started (how to run)
        - ## Deploy (how to ship)
     c. If any section missing OR README is just spawner-default →
        generate a proper README using:
        - project-truth from shared_knowledge["project-truth/<slug>"]
        - decisions/tech-stack.md
        - actual file tree (entry points)
        - recent commit messages (what's been built)
     d. Write README.md → git commit + push
  2. Track last update via shared_kv["readme-keeper.<slug>.ts"] so we
     don't re-write a perfect README every cycle.
"""
from __future__ import annotations
import datetime
import json
import os
import re
import signal
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, daemon_loop, call_llm,  # noqa: E402
                             get_portfolio)

POLL_SEC = int(os.environ.get("README_KEEPER_POLL_SEC", "300"))
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


def _sh(cmd: list[str], cwd: str | Path | None = None,
        t: int = 30, input_data: str | None = None
        ) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=t,
            cwd=cwd, input=input_data)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def fetch_project_truth(slug: str) -> dict:
    if not (SB_URL and SB_KEY):
        return {}
    import urllib.request, urllib.parse
    try:
        qs = urllib.parse.urlencode({
            "slug": f"eq.project-truth/{slug}",
            "select": "body,metadata",
        })
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/shared_knowledge?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read())
        if not rows:
            return {}
        try:
            return json.loads(rows[0].get("body", "{}"))
        except Exception:
            return {}
    except Exception:
        return {}


REQUIRED_SECTIONS = ("## Overview", "## Tech stack", "## Project structure",
                     "## Getting started", "## Deploy")


def needs_rewrite(readme: str) -> tuple[bool, list[str]]:
    """Returns (rewrite_needed, missing_sections)."""
    if not readme or len(readme) < 200:
        return True, list(REQUIRED_SECTIONS)
    # Spawner default detection — single short line
    if readme.count("\n") < 3:
        return True, list(REQUIRED_SECTIONS)
    missing = [s for s in REQUIRED_SECTIONS
               if s.lower() not in readme.lower()]
    return (len(missing) > 1), missing


def build_repo_summary(repo: Path) -> dict:
    """Gather facts about the repo for the README generator."""
    summary = {"slug": repo.name, "files": [], "dirs": [],
               "stack": "", "recent_commits": [], "entry_points": []}
    try:
        for p in sorted(repo.iterdir())[:30]:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                summary["dirs"].append(p.name)
            else:
                summary["files"].append(p.name)
        # Tech-stack lock
        ts = repo / "decisions" / "tech-stack.md"
        if ts.exists():
            summary["stack"] = ts.read_text(errors="replace")[:1500]
        # Recent commit subjects (last 8)
        rc, out, _ = _sh(["git", "log", "--oneline", "-n", "8"],
                         cwd=str(repo), t=10)
        if rc == 0:
            summary["recent_commits"] = out.strip().splitlines()
        # Entry points: main.py / app.ts / package.json scripts / etc.
        for ep_name in ("main.py", "main.go", "main.ts", "main.rs",
                        "app.py", "app.ts", "app.go", "index.ts",
                        "index.js", "server.py", "package.json",
                        "pyproject.toml", "Cargo.toml", "go.mod",
                        "Dockerfile", "docker-compose.yml"):
            if (repo / ep_name).exists():
                summary["entry_points"].append(ep_name)
    except Exception:
        pass
    return summary


README_SYSTEM = (
    "You are a senior technical writer modeling on the Firecrawl/Vercel "
    "open-source README style: badge-rich, emoji-anchored, bold-pitch. "
    "Given project facts, output a professional README.md with this layout:\n\n"
    "1. **Centered logo placeholder + emoji** in a single H3 — "
    "`<h3 align=\"center\">🛠️ <project-name></h3>`\n"
    "2. **Centered shields.io badges** (license MIT, language, build, "
    "stars) in a `<div align=\"center\">`\n"
    "3. `---` divider\n"
    "4. `# 🚀 <project-name>` H1 + ONE bold-pitch line — \"**Power "
    "<audience> with <verb> <noun>.** <product elevator pitch>\"\n"
    "5. `## Why <project-name>?` — bullet list (5-7 items) starting "
    "each bullet with a bold trait then dash:\n"
    "   - **Trait one**: 1-line concrete claim with a measurable signal\n"
    "   - **Built for X**: mention exact target use-case\n"
    "6. `## Feature Overview` — markdown table of features × description\n"
    "7. `## Tech Stack` — bullets matching `decisions/tech-stack.md` "
    "verbatim. NEVER add a stack not in the lock.\n"
    "8. `## Project Structure` — tree-style listing of top-level dirs "
    "with 1-line each\n"
    "9. `## Getting Started` — code blocks with EXACT commands "
    "(install/run/test) matching the locked stack\n"
    "10. `## Deploy` — code blocks for the deploy target in tech-stack.md\n"
    "11. `## Status` — 1-line + recent commit summary\n"
    "12. `## Contributing` — link to CONTRIBUTING.md placeholder\n"
    "13. `## License` — line stating license\n\n"
    "Style rules:\n"
    "- Use emoji headers (🚀 ⚡ 🔥 🛡️ 📦 🔧) sparingly — 1 per major H2\n"
    "- Bold the elevator-pitch line\n"
    "- Code blocks MUST be runnable, real paths\n"
    "- 400-900 words total\n"
    "- NEVER invent a stack — match the lock\n"
    "- Output FULL markdown only (no preamble, no JSON)")


def generate_readme(slug: str, truth: dict, summary: dict) -> str | None:
    portfolio_desc = ""
    try:
        pf = get_portfolio()
        portfolio_desc = pf.get(slug, "")
    except Exception:
        pass
    prompt = (
        f"Generate README.md for project: {slug}\n\n"
        f"## Portfolio description\n{portfolio_desc}\n\n"
        f"## Project truth (from codebase-indexer)\n"
        f"{json.dumps(truth, ensure_ascii=False, indent=2)[:1500]}\n\n"
        f"## Repo facts\n"
        f"- top-level dirs: {', '.join(summary['dirs'])}\n"
        f"- top-level files: {', '.join(summary['files'])}\n"
        f"- entry points: {', '.join(summary['entry_points'])}\n\n"
        f"## Tech-stack lock (decisions/tech-stack.md)\n"
        f"{summary['stack'] or '(not yet locked)'}\n\n"
        f"## Recent commits (last 8)\n"
        + "\n".join(f"  {c}" for c in summary['recent_commits'])
        + "\n\nWrite a great README. Concrete commands. Match the locked "
          "tech stack — do NOT invent a different stack."
    )
    try:
        out = call_llm(prompt, system=README_SYSTEM,
                       max_tokens=2000, timeout=60)
        # Strip code-fence if LLM wrapped output
        txt = out.strip()
        if txt.startswith("```"):
            txt = txt.split("\n", 1)[1] if "\n" in txt else txt
            if txt.endswith("```"):
                txt = txt.rsplit("```", 1)[0]
        return txt.strip() or None
    except Exception as e:
        log("readme-keeper",
            f"  ✗ {slug} LLM: {type(e).__name__}: {str(e)[:60]}")
        return None


def commit_readme(repo: Path, content: str) -> bool:
    readme = repo / "README.md"
    try:
        readme.write_text(content)
    except Exception:
        # Try via sudo if permission issue
        rc, _, _ = _sh(["sudo", "tee", str(readme)],
                       input_data=content, t=10)
        if rc != 0:
            return False
    _sh(["sudo", "git", "config", "user.email", "tech-lead@axentx.local"],
        cwd=str(repo), t=5)
    _sh(["sudo", "git", "config", "user.name", "axentx-readme-keeper"],
        cwd=str(repo), t=5)
    rc, out, err = _sh(
        ["sudo", "git", "add", "README.md"], cwd=str(repo), t=10)
    rc, out, err = _sh(
        ["sudo", "git", "commit", "-m",
         "readme-keeper: generate proper project README "
         "(overview/stack/run/deploy)"],
        cwd=str(repo), t=15)
    if rc != 0:
        # nothing to commit is OK
        if "nothing to commit" in (out + err):
            return False
    rc, _, _ = _sh(["sudo", "git", "push", "origin", "HEAD:main"],
                   cwd=str(repo), t=60)
    return rc == 0


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("readme-keeper", "  ⤷ not leader — skip")
        return False
    if not PROJECTS_ROOT.exists():
        return False

    repos = [p for p in PROJECTS_ROOT.iterdir()
             if p.is_dir() and (p / ".git").exists()
             and p.name not in {"cost-radar", "arkship"}]
    if not repos:
        return False

    written = 0
    for repo in repos:
        slug = repo.name
        readme_path = repo / "README.md"
        readme = ""
        try:
            if readme_path.exists():
                readme = readme_path.read_text(errors="replace")
        except Exception:
            pass
        rewrite, missing = needs_rewrite(readme)
        if not rewrite:
            continue
        log("readme-keeper",
            f"▸ {slug}: rewrite needed (missing: "
            f"{', '.join(missing) or 'thin readme'})")
        truth = fetch_project_truth(slug)
        summary = build_repo_summary(repo)
        new_readme = generate_readme(slug, truth, summary)
        if not new_readme or len(new_readme) < 300:
            log("readme-keeper", f"  ⊘ {slug}: LLM returned thin output, skip")
            continue
        if commit_readme(repo, new_readme):
            written += 1
            log("readme-keeper",
                f"  ✓ {slug}: README committed ({len(new_readme)} chars)")
            try:
                from axentx_shared import kv_set, memory_log
                kv_set(f"readme-keeper.{slug}", {
                    "ts": datetime.datetime.utcnow().isoformat() + "Z",
                    "len": len(new_readme), "host": HOST,
                })
                memory_log("readme-keeper", "readme-written",
                           f"{slug} README updated",
                           body=new_readme[:1500],
                           tags=["readme-keeper", slug])
            except Exception:
                pass
        if written >= 3:
            break   # cap per cycle to avoid LLM storm

    log("readme-keeper", f"  ✓ wrote {written} README(s) this cycle")
    return False


if __name__ == "__main__":
    daemon_loop("readme-keeper", POLL_SEC, cycle)
