#!/usr/bin/env python3
"""axentx repo-sync — ensures every host has every project repo cloned
under /opt/axentx/<slug>/. Pulls updates periodically.

User feedback 2026-05-04:
  > 'ไม่ได้ sync เหรอ แล้วไม่รู้เหรอว่ามี product อะไรบ้าง — ไหนบอกแก้แล้ว'

Cycle (10min, runs on EVERY host — not leader-only since each host
needs its own local copies):
  1. Read shared_kv["bd.portfolio"] → list of slugs
  2. For each slug not in /opt/axentx/<slug>/:
     - Try owners in priority order (arkashira, ashirapit, ashirafuse,
       axentx-tech, etc.) using their respective tokens
     - sudo git clone --depth 1 first match
  3. For each existing /opt/axentx/<slug>/.git:
     - sudo git fetch + reset --hard origin/main (idempotent latest)
     - skip if local has uncommitted changes (dev work in progress)
  4. Log to memory_log on first-clone events (peers see new repos arrive)
"""
from __future__ import annotations
import datetime
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("REPO_SYNC_POLL_SEC", "600"))
HOST = socket.gethostname()
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))

# Owner-token map. Each ashirapit/arkashira/etc. has its own PAT in env.
# Try in priority order — first that succeeds wins for the slug.
def _owner_token_pairs() -> list[tuple[str, str]]:
    """Build (owner, token) candidate list from env. Tokens we have:
    GITHUB_TOKEN (default), GITHUB_TOKEN_ARKASHIRA, GITHUB_TOKEN_ASHIRAPIT,
    + GITHUB_TOKEN_POOL (comma-separated extras)."""
    out: list[tuple[str, str]] = []
    primary = os.environ.get("GITHUB_TOKEN", "").strip()
    ark = os.environ.get("GITHUB_TOKEN_ARKASHIRA", "").strip() or primary
    pit = os.environ.get("GITHUB_TOKEN_ASHIRAPIT", "").strip() or primary
    if ark: out.append(("arkashira", ark))
    if pit: out.append(("ashirapit", pit))
    # Other org owners — try with primary token (if it has access)
    for owner in ("ashirafuse", "axentx-tech", "arkship-ai",
                  "luckyburster-lab", "midnightgts", "ifusefreedomza",
                  "surrogate-1"):
        if primary:
            out.append((owner, primary))
    # Append all pool tokens too — different scope/org membership
    pool = os.environ.get("GITHUB_TOKEN_POOL", "").strip()
    for t in pool.split(","):
        t = t.strip()
        if t and len(t) > 30:
            for owner in ("arkashira", "ashirapit"):
                out.append((owner, t))
    return out


_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _sh(cmd: list[str], t: int = 30) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=t)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def fetch_portfolio_slugs() -> list[str]:
    try:
        from axentx_shared import kv_get
        v = kv_get("bd.portfolio") or {}
        if isinstance(v, dict) and v.get("v"): v = v["v"]
        products = (v.get("products") or {}) if isinstance(v, dict) else {}
        # Skip legacy/archived
        return [s for s in products
                if s not in {"arkship", "cost-radar"}]
    except Exception:
        return []


def clone_missing(slug: str) -> tuple[bool, str]:
    """Try owners in order. Returns (success, owner_or_msg)."""
    target = PROJECTS_ROOT / slug
    if target.exists():
        return False, "already exists"
    for owner, token in _owner_token_pairs():
        url = f"https://x-access-token:{token}@github.com/{owner}/{slug}"
        rc, _, err = _sh(
            ["sudo", "git", "clone", "--depth", "1", url, str(target)],
            t=120)
        if rc == 0 and target.exists():
            return True, owner
        # Clean up half-clone if exists
        if target.exists():
            _sh(["sudo", "rm", "-rf", str(target)], t=10)
    return False, "no owner matched"


def pull_existing(repo: Path) -> bool:
    """Idempotent fast-forward pull. Skip if dirty (dev mid-edit)."""
    rc, out, _ = _sh(
        ["sudo", "git", "-C", str(repo), "status", "--porcelain"], t=10)
    if rc != 0:
        return False
    if out.strip():
        return False   # dirty — skip to avoid clobbering local work
    rc, _, _ = _sh(
        ["sudo", "git", "-C", str(repo), "fetch", "origin"], t=30)
    if rc != 0:
        return False
    rc, _, _ = _sh(
        ["sudo", "git", "-C", str(repo), "reset", "--hard",
         "origin/main"], t=15)
    return rc == 0


def cycle():
    if _stop:
        return False
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)

    slugs = fetch_portfolio_slugs()
    if not slugs:
        log("repo-sync", "  ⊘ no portfolio yet")
        return False

    cloned = 0
    pulled = 0
    failed = 0
    new_clones: list[str] = []
    for slug in slugs:
        target = PROJECTS_ROOT / slug
        if not target.exists():
            ok, owner = clone_missing(slug)
            if ok:
                cloned += 1
                new_clones.append(f"{owner}/{slug}")
                log("repo-sync", f"  ✓ cloned {owner}/{slug}")
            else:
                failed += 1
        else:
            if pull_existing(target):
                pulled += 1

    if cloned:
        try:
            from axentx_shared import memory_log
            memory_log("repo-sync", "cloned-repos",
                       f"cloned {cloned} new repo(s) on {HOST}",
                       body="\n".join(new_clones),
                       tags=["repo-sync", HOST])
        except Exception:
            pass

    log("repo-sync",
        f"  ✓ portfolio={len(slugs)}, cloned+{cloned}, pulled={pulled}, "
        f"failed={failed}")
    return False


if __name__ == "__main__":
    daemon_loop("repo-sync", POLL_SEC, cycle)
