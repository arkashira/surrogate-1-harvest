#!/usr/bin/env python3
"""state-sync — periodically push runtime state to git so any VM can rebuild.

User directive (2026-05-02):
  > "config ทั้งหมด ของทุกที push ไปเก็บบนgit เรื่อยๆนะ เมื่อทำผิด จะได้
  >  ไปเอากลับมาได้ และรู้ได้ว่าเพราะอะไรถึงหาย เอากลับมาได้ทุกเมื่อ"
  > "context ส่วนกลาง knowledge ส่วนกลาง memory ส่วนกลาง ต้อง sync ไป
  >  เก็บข้างนอก ... ไม่งั้น service crash หรือ มี vm ใหม่ มันจะทำงาน
  >  ร่วมกันไม่ได้"

What gets synced (every SYNC_SEC):
  /opt/surrogate-1-harvest/state/swarm-shared/decisions/
  /opt/surrogate-1-harvest/state/swarm-shared/sprint/
  /opt/surrogate-1-harvest/state/swarm-shared/retro/
  /opt/surrogate-1-harvest/state/swarm-shared/past-cycles/
  /opt/surrogate-1-harvest/state/swarm-shared/AGENT_RULES.md
  /opt/surrogate-1-harvest/state/swarm-shared/chains.json
  /opt/surrogate-1-harvest/state/chat-memory/profiles.json
  /opt/surrogate-1-harvest/state/chat-memory/history/
  /opt/surrogate-1-harvest/state/.cursors/        (research/dev/seen)
  /opt/surrogate-1-harvest/state/self-heal/       (attempt counters)
  /etc/systemd/system/axentx-*.service            (unit defs)
  /etc/systemd/system/surrogate-*.service
  /etc/systemd/system/hermes-*.service

What does NOT get synced (intentionally):
  done/   — too large; if needed for archaeology, training-pairs.jsonl
            already drains the signal we care about.
  Live queues (dev-queue, review-queue, etc.) — these are work-in-flight,
            not knowledge. Re-priming on a fresh VM happens via the
            research → bd → ... pipeline naturally.
  /etc/surrogate-coordinator.env — has secrets in plaintext.

Where it goes:
  Orphan branch `state` in arkashira/surrogate-1-harvest. One commit per
  sync cycle, message includes counts diff. Force-push so the branch
  doesn't grow unboundedly (we only need the latest snapshot + recent
  history; git keeps the history naturally).

Architecture:
  /opt/surrogate-1-state/   — separate git checkout pinned to `state`
                              branch. We rsync into it, commit, push.
  Idempotent: if nothing changed, no commit.
  Restart-safe: cursor at /opt/surrogate-1-harvest/state/.state-sync.json.
"""
from __future__ import annotations

import datetime
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
SNAPSHOT_DIR = Path(os.environ.get(
    "STATE_SNAPSHOT_DIR", "/opt/surrogate-1-state"))
LOG_FILE = REPO_ROOT / "logs" / "state-sync.log"
SYNC_SEC = int(os.environ.get("STATE_SYNC_SEC", "300"))  # 5 min
REMOTE_NAME = os.environ.get("STATE_SYNC_REMOTE", "origin")
BRANCH = os.environ.get("STATE_SYNC_BRANCH", "state")
ORIGIN_REPO = os.environ.get(
    "STATE_SYNC_REPO_URL",
    "https://github.com/arkashira/surrogate-1-harvest.git",
)
GIT_USER = os.environ.get("GIT_USER_NAME", "axentx-state-bot")
GIT_EMAIL = os.environ.get("GIT_USER_EMAIL", "state-bot@axentx.local")

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] [state-sync] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def run(cmd: list[str], cwd: Path | None = None,
        check: bool = True, env: dict | None = None,
        timeout: int = 120) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          check=check, env=full_env, timeout=timeout)


def shutdown(*_):
    log("shutdown")
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def _authed_remote() -> str:
    """Inject GITHUB_TOKEN into the HTTPS URL so push/clone don't prompt.

    Prefer GITHUB_TOKEN_ARKASHIRA (org-scoped) over generic GITHUB_TOKEN —
    the org token is what the rest of the fleet (commit-daemon etc.) uses.
    """
    tok = (os.environ.get("GITHUB_TOKEN_ARKASHIRA")
           or os.environ.get("AXENTX_BOT_GITHUB_TOKEN")
           or os.environ.get("GITHUB_TOKEN", ""))
    if tok and ORIGIN_REPO.startswith("https://"):
        rest = ORIGIN_REPO[len("https://"):]
        return f"https://x-access-token:{tok}@{rest}"
    return ORIGIN_REPO


def ensure_snapshot_repo() -> None:
    """Idempotent: clone the snapshot repo + create orphan `state` branch.

    Runs every cycle (cheap when already set up). Side-effect-free if the
    snapshot dir is healthy; recovers from any partial state gracefully.
    """
    authed = _authed_remote()
    git_dir = SNAPSHOT_DIR / ".git"
    if not git_dir.exists():
        log(f"cloning → {SNAPSHOT_DIR}")
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        # Empty dir is fine for `git clone` only if used as target with no
        # contents. If the dir is empty, clone with `.` as target. Otherwise
        # nuke contents first (we own this path).
        if any(SNAPSHOT_DIR.iterdir()):
            for entry in SNAPSHOT_DIR.iterdir():
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        run(["git", "clone", "--depth=1", authed, "."], cwd=SNAPSHOT_DIR)
    # Always rewrite remote URL with fresh token (avoids stale 90-day token
    # being baked into config from a previous clone).
    run(["git", "remote", "set-url", REMOTE_NAME, authed],
        cwd=SNAPSHOT_DIR, check=False)
    # Configure local identity (push will fail without it on fresh clones)
    run(["git", "config", "user.name", GIT_USER], cwd=SNAPSHOT_DIR)
    run(["git", "config", "user.email", GIT_EMAIL], cwd=SNAPSHOT_DIR)
    # Ensure local branch tracks remote `state`. If remote `state` doesn't
    # exist yet, create an orphan branch on first push.
    branches = run(["git", "branch", "-a"], cwd=SNAPSHOT_DIR, check=False)
    has_remote_state = f"remotes/{REMOTE_NAME}/{BRANCH}" in (branches.stdout or "")
    has_local_state = f"  {BRANCH}\n" in (branches.stdout or "") or \
                      f"* {BRANCH}\n" in (branches.stdout or "")
    if has_local_state:
        run(["git", "checkout", BRANCH], cwd=SNAPSHOT_DIR, check=False)
    elif has_remote_state:
        run(["git", "checkout", "-b", BRANCH,
             f"{REMOTE_NAME}/{BRANCH}"], cwd=SNAPSHOT_DIR)
    else:
        # First-run bootstrap: orphan branch with empty content.
        log(f"bootstrapping orphan branch '{BRANCH}'")
        run(["git", "checkout", "--orphan", BRANCH], cwd=SNAPSHOT_DIR, check=False)
        # Wipe staged + working tree
        run(["git", "rm", "-rf", "--cached", "."], cwd=SNAPSHOT_DIR, check=False)
        for entry in SNAPSHOT_DIR.iterdir():
            if entry.name == ".git":
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        readme = SNAPSHOT_DIR / "README.md"
        readme.write_text(
            "# surrogate-1 runtime state\n\n"
            "Auto-synced by state-sync-daemon every "
            f"{SYNC_SEC}s. Restoring a VM:\n"
            "```\n"
            "git clone -b state https://github.com/arkashira/surrogate-1-harvest.git\n"
            "rsync -a state-snapshot/ /opt/surrogate-1-harvest/state/\n"
            "```\n"
            "Latest sync timestamp lives in .last-sync.\n"
        )
        run(["git", "add", "README.md"], cwd=SNAPSHOT_DIR)
        run(["git", "commit", "-m", "bootstrap state branch"],
            cwd=SNAPSHOT_DIR, check=False)


def rsync_state() -> None:
    """Mirror selected paths into the snapshot working tree."""
    targets = [
        # Decisions / sprint / retro / cycles / agent rules
        (REPO_ROOT / "state" / "swarm-shared" / "decisions",
         SNAPSHOT_DIR / "swarm-shared" / "decisions"),
        (REPO_ROOT / "state" / "swarm-shared" / "sprint",
         SNAPSHOT_DIR / "swarm-shared" / "sprint"),
        (REPO_ROOT / "state" / "swarm-shared" / "retro",
         SNAPSHOT_DIR / "swarm-shared" / "retro"),
        (REPO_ROOT / "state" / "swarm-shared" / "past-cycles",
         SNAPSHOT_DIR / "swarm-shared" / "past-cycles"),
        # Multi-user chat memory
        (REPO_ROOT / "state" / "chat-memory",
         SNAPSHOT_DIR / "chat-memory"),
        # Self-heal counters (so we can debug recurring restart patterns)
        (REPO_ROOT / "state" / "self-heal",
         SNAPSHOT_DIR / "self-heal"),
    ]
    files = [
        (REPO_ROOT / "state" / "swarm-shared" / "AGENT_RULES.md",
         SNAPSHOT_DIR / "swarm-shared" / "AGENT_RULES.md"),
        (REPO_ROOT / "state" / "swarm-shared" / "chains.json",
         SNAPSHOT_DIR / "swarm-shared" / "chains.json"),
    ]
    for src, dst in targets:
        if not src.exists():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        # Use rsync to mirror with deletes — drops files that no longer
        # exist on the live side so the snapshot reflects truth.
        run(["rsync", "-a", "--delete", "--exclude=.tmp",
             "--exclude=*.corrupt", str(src) + "/", str(dst) + "/"],
            check=False)
    for src, dst in files:
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    # Cursors — flat JSONs in state/
    cursors_dst = SNAPSHOT_DIR / "cursors"
    cursors_dst.mkdir(parents=True, exist_ok=True)
    for cursor in (REPO_ROOT / "state").glob("axentx-*-cursor*.json"):
        shutil.copy2(cursor, cursors_dst / cursor.name)
    for sentinel in (REPO_ROOT / "state").glob(".*-cursor*.json"):
        shutil.copy2(sentinel, cursors_dst / sentinel.name)
    # Systemd unit defs (drop .service files only — .timer + others too)
    units_dst = SNAPSHOT_DIR / "systemd"
    units_dst.mkdir(parents=True, exist_ok=True)
    sys_dir = Path("/etc/systemd/system")
    for pattern in ("axentx-*", "surrogate-*", "hermes-*"):
        for unit in sys_dir.glob(pattern):
            if unit.is_file():
                try:
                    shutil.copy2(unit, units_dst / unit.name)
                except PermissionError:
                    continue
    # Marker file with timestamp — guarantees a delta even if everything
    # else is unchanged so we have heartbeat commits proving the daemon
    # is alive.
    (SNAPSHOT_DIR / ".last-sync").write_text(
        datetime.datetime.utcnow().isoformat() + "Z\n"
    )


def commit_and_push() -> bool:
    """Stage everything, commit if there's a real diff, push. Returns True
    if a commit was made.

    Multi-writer safety: BOTH GCP and Kamatera run state-sync-daemon. They
    push to the same `state` branch, so we MUST pull-rebase before push.
    With rsync filling the working tree, conflicts are unlikely (each VM
    contributes its own host-tagged history) but `git pull --rebase` keeps
    the branch linear regardless.
    """
    run(["git", "add", "-A"], cwd=SNAPSHOT_DIR)
    diff = run(["git", "diff", "--cached", "--stat"],
               cwd=SNAPSHOT_DIR, check=False)
    if not (diff.stdout or "").strip():
        return False
    # Count what changed for the commit message
    name_status = run(["git", "diff", "--cached", "--name-only"],
                      cwd=SNAPSHOT_DIR, check=False)
    n_files = len([ln for ln in (name_status.stdout or "").splitlines() if ln])
    import socket as _s
    host = _s.gethostname()
    msg = (f"state sync: {n_files} files updated @ "
           f"{datetime.datetime.utcnow().isoformat()}Z [{host}]")
    run(["git", "commit", "-m", msg], cwd=SNAPSHOT_DIR)
    # Push, with one pull-rebase retry on rejection (multi-writer race).
    for attempt in (1, 2, 3):
        push = run(["git", "push", "-u", REMOTE_NAME, BRANCH],
                   cwd=SNAPSHOT_DIR, check=False, timeout=60)
        if push.returncode == 0:
            return True
        # Rejection — try fetch + rebase + push again
        log(f"  push attempt {attempt} rejected — pull-rebase + retry")
        run(["git", "fetch", REMOTE_NAME, BRANCH],
            cwd=SNAPSHOT_DIR, check=False, timeout=30)
        rebase = run(["git", "rebase", f"{REMOTE_NAME}/{BRANCH}"],
                     cwd=SNAPSHOT_DIR, check=False, timeout=30)
        if rebase.returncode != 0:
            # Conflict — abort rebase and reset hard to remote.
            # Our local changes will get re-rsync'd next cycle anyway, so
            # discarding them is safe (state is regenerable from /opt).
            run(["git", "rebase", "--abort"], cwd=SNAPSHOT_DIR, check=False)
            run(["git", "reset", "--hard", f"{REMOTE_NAME}/{BRANCH}"],
                cwd=SNAPSHOT_DIR, check=False)
            log("  rebase conflict — reset to remote, will re-sync next cycle")
            return False
        time.sleep(1 + attempt)  # back-off
    log(f"  push failed after 3 attempts: {(push.stderr or '')[:200]}")
    return False


def sync_once() -> int:
    """One full cycle. Returns # files committed (0 = nothing changed)."""
    ensure_snapshot_repo()
    rsync_state()
    pre = run(["git", "diff", "--cached", "--name-only"],
              cwd=SNAPSHOT_DIR, check=False).stdout
    pushed = commit_and_push()
    if pushed:
        n = len([ln for ln in (pre or "").splitlines() if ln])
        return n
    return 0


def main() -> int:
    log(f"start — sync every {SYNC_SEC}s → {ORIGIN_REPO}#{BRANCH}")
    n = 0
    while True:
        n += 1
        try:
            changed = sync_once()
            if changed:
                log(f"cycle #{n}: pushed snapshot ({changed} files)")
            elif n % 12 == 1:
                log(f"cycle #{n}: no changes (heartbeat)")
        except Exception as e:
            log(f"⚠ cycle #{n} error: {type(e).__name__}: {str(e)[:200]}")
        time.sleep(SYNC_SEC)


if __name__ == "__main__":
    sys.exit(main())
