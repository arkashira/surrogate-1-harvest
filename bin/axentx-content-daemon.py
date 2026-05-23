#!/usr/bin/env python3
"""axentx content — drafts blog posts + social copy from shipped commits.

Periodic (default every 4h): scans last 4h of commits per project,
groups by theme (LLM-clustered), drafts:
- blog post (markdown, 600-1000 words)
- 3 short social posts (Twitter/LinkedIn)
- changelog summary
Saves under docs/blog/<date>/<slug>.md and pushes via commit-queue."""
from __future__ import annotations
import datetime, json, os, sys, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, daemon_loop, new_item, write_item)
# 5 min poll — was 4h. Continuous content-from-commits drafting; the inner
# do_one_cycle() already filters to "last N hours of commits" so over-firing
# is cheap (returns False, no LLM call). User directive 2026-05-02 round 3.
POLL_SEC = int(os.environ.get("CONTENT_POLL_SEC", "300"))
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
PROJECTS = ["Costinel", "vanguard", "airship", "workio", "surrogate-1"]
# axiomops dropped 2026-05-02 — merged into airship

CONTENT_SYSTEM = """You are a developer-marketing writer. Given a list of
recent commits in a project, produce:

{
  "theme": "<one-line theme of the work>",
  "blog_md": "<600-1000 word markdown post — title h1, subheads, code blocks where relevant>",
  "twitter": ["<280-char tweet>", "<...>", "<...>"],
  "linkedin": "<300-500 word professional post>",
  "changelog": "<short bullet summary>"
}

Voice: technical, no fluff, link-baity title is fine but body must deliver.
Audience: engineers building similar things."""

def git(repo, *args, timeout=15):
    return subprocess.run(["git","-C",str(repo),*args],
                          capture_output=True, text=True, timeout=timeout)

def do_one() -> bool:
    fired = 0
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    for proj in PROJECTS:
        repo = PROJECTS_ROOT / proj
        if not (repo / ".git").exists(): continue
        # commits in last 4h
        r = git(repo, "log", "--since=4 hours ago",
                "--pretty=format:%h %s", "-50")
        if r.returncode != 0 or not r.stdout.strip(): continue
        commits = r.stdout.strip().splitlines()
        if len(commits) < 3: continue  # need critical mass
        log("content", f"▸ {proj}: {len(commits)} commits in last 4h")
        try:
            out = call_llm(
                f"Project: {proj}\nRecent commits:\n" + "\n".join(commits),
                system=CONTENT_SYSTEM, max_tokens=2500, timeout=90)
            txt = out.strip()
            if "```" in txt: txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
            c = json.loads(txt.strip())
        except Exception as e:
            log("content", f"  ⚠ {proj} fail: {e}"); continue
        # write markdown
        slug = (c.get("theme","update")[:60].replace(" ","-")
                  .replace("/","-").lower())
        blog_dir = repo / "docs" / "blog" / today
        blog_dir.mkdir(parents=True, exist_ok=True)
        (blog_dir / f"{slug}.md").write_text(
            f"---\ndate: {today}\ntheme: {c.get('theme','')}\n---\n\n" +
            c.get("blog_md","") + "\n\n## Tweets\n" +
            "\n".join(f"- {t}" for t in c.get("twitter",[])) + "\n\n## LinkedIn\n" +
            c.get("linkedin","") + "\n"
        )
        # push via commit-queue (commit daemon handles git add+commit+push)
        ci = new_item(proj, "content", f"blog: {c.get('theme','')[:40]}")
        ci["history"] = [{"stage":"dev","actor":"axentx-content",
                          "output":f"Auto-content from {len(commits)} commits.\n\n"
                                   f"File: docs/blog/{today}/{slug}.md\n\n" +
                                   c.get("blog_md","")[:1500],
                          "at": datetime.datetime.utcnow().isoformat()+"Z"}]
        ci["current"]["text"] = ci["history"][0]["output"]
        write_item(ci, "commit")
        fired += 1
        log("content", f"  ✓ blog drafted: {slug}")
    return fired > 0

if __name__ == "__main__":
    daemon_loop("content", POLL_SEC, do_one)
