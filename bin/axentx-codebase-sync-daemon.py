#!/usr/bin/env python3
"""axentx-codebase-sync — pull every /opt/axentx/* repo every 10 min.

Goal: keep Kam1 + Kam2 codebase in sync via GitHub. Each VM runs this;
they pull each other's commits on cycle.

Skips: archived repos, repos without origin (null + biz-plans).

2026-05-09 — created to fix cross-VM divergence (Kam2 had 25 dirs,
Kam1 had 16, with different products on each).
"""
from __future__ import annotations
import os, signal, subprocess, sys, time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log

POLL_SEC = int(os.environ.get("CODEBASE_SYNC_POLL_SEC", "600"))   # 10 min
PROJECTS = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))

# Known archived — don't pull (push will fail anyway)
ARCHIVED = {"sync-keeper", "cloud-pilot", "quote-trail", "cost-radar"}

_stop = False


def _signal(*_):
    global _stop
    _stop = True
    log("codebase-sync", "shutdown signal")


signal.signal(signal.SIGTERM, _signal)
signal.signal(signal.SIGINT, _signal)


def run(cmd, cwd=None, timeout=120):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                      timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def sync_one(repo_dir: Path) -> tuple[str, str]:
    """Try git pull. Returns (status, detail). # 2026-05-09 robust sync v2

    Strategy:
      1. Skip if archived OR no .git OR no origin
      2. Abort any in-progress rebase/merge (recover stuck repos)
      3. Fetch with --no-tags first (tags often conflict on aggressive sync)
      4. If local is BEHIND origin/main: fast-forward merge
      5. If local has unpushed commits: SKIP (let commit-daemon push first)
      6. Never auto-rebase — too risky for diverged trees
    """
    if repo_dir.name in ARCHIVED:
        return "skip-archived", repo_dir.name
    if not (repo_dir / ".git").is_dir():
        return "skip-nogit", ""
    rc, out, _ = run(["git", "-C", str(repo_dir), "remote", "get-url", "origin"])
    if rc != 0 or not out.strip():
        return "skip-noorigin", ""

    # Recover from stuck rebase/merge state
    run(["git", "-C", str(repo_dir), "rebase", "--abort"])
    run(["git", "-C", str(repo_dir), "merge", "--abort"])
    run(["git", "-C", str(repo_dir), "cherry-pick", "--abort"])

    # Detect default remote branch (main or master)
    rc, head_out, _ = run([
        "git", "-C", str(repo_dir), "remote", "show", "origin",
    ], timeout=15)
    default_branch = "main"
    for line in head_out.splitlines():
        if "HEAD branch:" in line:
            default_branch = line.split(":", 1)[1].strip()
            break

    # Fetch (no tags to avoid clutter)
    rc, out, err = run(
        ["git", "-C", str(repo_dir), "fetch", "--no-tags", "origin", default_branch],
        timeout=60,
    )
    if rc != 0:
        return "fetch-fail", err[:160]

    # Compare local HEAD vs origin
    rc, ahead_out, _ = run([
        "git", "-C", str(repo_dir), "rev-list", "--left-right", "--count",
        f"HEAD...origin/{default_branch}",
    ])
    if rc != 0:
        return "rev-list-fail", ""
    parts = ahead_out.strip().split()
    if len(parts) != 2:
        return "rev-list-bad", ahead_out[:80]
    ahead, behind = int(parts[0]), int(parts[1])

    if ahead == 0 and behind == 0:
        return "uptodate", ""
    if ahead > 0:
        # Local has unpushed commits — let commit-daemon handle, skip
        return "skip-ahead", f"{ahead} unpushed"
    if behind > 0:
        # Fast-forward merge (safe — no local divergence)
        rc, out, err = run([
            "git", "-C", str(repo_dir), "merge", "--ff-only",
            f"origin/{default_branch}",
        ], timeout=30)
        if rc == 0:
            return "pulled", f"FF {behind} commits"
        return "ff-fail", err[:160]
    return "noop", ""


def cycle():
    if not PROJECTS.is_dir():
        log("codebase-sync", f"no PROJECTS dir at {PROJECTS}; skip")
        return
    pulled = 0
    skipped = 0
    failed = 0
    for entry in sorted(PROJECTS.iterdir()):
        if _stop:
            return
        if not entry.is_dir():
            continue
        status, detail = sync_one(entry)
        if status == "pulled":
            pulled += 1
            log("codebase-sync", f"  ↻ {entry.name}: {detail}")
        elif status == "fail":
            failed += 1
            log("codebase-sync", f"  ✗ {entry.name}: {detail}")
        elif status.startswith("skip"):
            skipped += 1
    log("codebase-sync",
        f"cycle done — pulled={pulled} skipped={skipped} failed={failed}")


def main():
    log("codebase-sync", f"start — poll {POLL_SEC}s, scan {PROJECTS}")
    while not _stop:
        try:
            cycle()
        except Exception as e:
            log("codebase-sync", f"cycle error: {type(e).__name__}: {e}")
        # interruptible sleep
        for _ in range(POLL_SEC):
            if _stop:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
