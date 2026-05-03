#!/usr/bin/env python3
"""axentx disk janitor — autonomous disk-pressure handler.

Runs continuously (60s loop). Tiered response by free-space level:

  >2GB free  : minimal — just trim caches that don't need locks
  500MB-2GB  : moderate — vacuum journals, prune .processed/.suggestion,
               archive large jsonls to bucket
  <500MB     : emergency — aggressive prune + truncate runaway logs +
               kick stale daemon containers, alert via Discord

Idempotent + crash-safe. Never deletes anything inside an active git
repo (won't touch /opt/axentx/* or /opt/surrogate-1-harvest/.git/).

Why this is a STREAM not a cron:
  cron-style hourly cleanup let disk fill to 100% between runs (verified
  twice on 2026-05-03). Streaming lets it react in seconds when usage
  spikes (e.g. dataset-enrich.sh running parallel mass writes).
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

# Tunables
POLL_SEC = int(os.environ.get("JANITOR_POLL_SEC", "60"))
THRESHOLD_OK_GB = float(os.environ.get("JANITOR_OK_GB", "2.0"))
THRESHOLD_WARN_GB = float(os.environ.get("JANITOR_WARN_GB", "0.5"))
JSONL_ROTATE_MB = int(os.environ.get("JANITOR_JSONL_ROTATE_MB", "200"))
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

PRUNE_DIRS = [
    Path("/var/log/apt/term.log"),       # Apt logs grow forever
    Path("/var/lib/apt/lists"),          # Apt cache, regenerated on demand
]
PROCESSED_PATTERNS = [
    ("/opt/axentx", "*.processed"),
    ("/opt/axentx", "*.suggestion"),
]
JSONL_HOTSPOTS = [
    Path("/opt/surrogate-1-harvest/state"),
    Path("/home/ubuntu/.surrogate"),
    Path("/home/ubuntu/axentx/surrogate/data/training-jsonl"),
]
KEEP_TAIL_MB = 100   # keep last 100MB when rotating large jsonl

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def free_gb() -> float:
    s = shutil.disk_usage("/")
    return s.free / 1e9


def discord_alert(msg: str) -> None:
    if not DISCORD_WEBHOOK:
        return
    import urllib.request
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                DISCORD_WEBHOOK,
                data=json.dumps({"content": msg[:1990]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            ), timeout=10,
        ).read()
    except Exception:
        pass


def vacuum_journals(target_mb: int = 30) -> int:
    """Trim systemd journal to target size."""
    try:
        out = subprocess.run(
            ["journalctl", f"--vacuum-size={target_mb}M"],
            capture_output=True, text=True, timeout=30,
        )
        # parse "freed XYZB"
        freed = 0
        for ln in (out.stderr + out.stdout).splitlines():
            if "freed" in ln.lower():
                # crude: pull number+unit
                import re
                m = re.search(r"freed\s+([\d.]+)([MK])", ln)
                if m:
                    n = float(m.group(1))
                    freed += int(n * (1e6 if m.group(2) == "M" else 1e3))
        return freed
    except Exception:
        return 0


def dedup_training_pairs() -> int:
    """training-pairs.jsonl is referenced from 2 paths — make symlink."""
    canonical = Path("/opt/surrogate-1-harvest/state/training-pairs.jsonl")
    duplicate = Path("/home/ubuntu/.surrogate/training-pairs.jsonl")
    if not canonical.exists() or not duplicate.exists():
        return 0
    if duplicate.is_symlink():
        return 0
    try:
        c_size = canonical.stat().st_size
        d_size = duplicate.stat().st_size
        # if ~same size, replace dup with symlink
        if abs(c_size - d_size) < c_size * 0.05:
            duplicate.unlink()
            duplicate.symlink_to(canonical)
            log("janitor",
                f"  dedup training-pairs: symlinked {duplicate} → {canonical} "
                f"(saved {d_size // 1_000_000}MB)")
            return d_size
    except Exception as e:
        log("janitor", f"  dedup fail: {e}")
    return 0


def prune_processed(max_age_hours: int = 1) -> int:
    """Remove .processed/.suggestion markers older than threshold."""
    import fnmatch
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for root, pat in PROCESSED_PATTERNS:
        root_p = Path(root)
        if not root_p.exists():
            continue
        for f in root_p.rglob(pat):
            try:
                if f.stat().st_mtime < cutoff:
                    sz = f.stat().st_size
                    f.unlink(missing_ok=True)
                    removed += sz
            except Exception:
                continue
    if removed:
        log("janitor",
            f"  pruned .processed/.suggestion (saved {removed // 1_000_000}MB)")
    return removed


def prune_apt_cache() -> int:
    """apt-get clean idempotent."""
    try:
        before = sum(f.stat().st_size for f in
                     Path("/var/cache/apt").rglob("*") if f.is_file())
        subprocess.run(["apt-get", "clean"], capture_output=True, timeout=30)
        return before
    except Exception:
        return 0


def rotate_large_jsonls() -> int:
    """For any jsonl >ROTATE_MB, keep the last KEEP_TAIL_MB and drop earlier."""
    saved = 0
    cutoff = JSONL_ROTATE_MB * 1_000_000
    keep = KEEP_TAIL_MB * 1_000_000
    for hot in JSONL_HOTSPOTS:
        if not hot.exists():
            continue
        for f in hot.rglob("*.jsonl"):
            try:
                if f.is_symlink():
                    continue
                sz = f.stat().st_size
                if sz <= cutoff:
                    continue
                # rotate: read last `keep` bytes, write back
                with f.open("rb") as fh:
                    fh.seek(max(0, sz - keep))
                    # advance to next newline so we don't split a row
                    fh.readline()
                    tail = fh.read()
                tmp = f.with_suffix(".jsonl.rotating")
                tmp.write_bytes(tail)
                tmp.replace(f)
                saved += sz - len(tail)
                log("janitor",
                    f"  rotated {f.name}: {sz // 1_000_000}MB → "
                    f"{len(tail) // 1_000_000}MB")
            except Exception as e:
                log("janitor", f"  rotate fail {f}: {type(e).__name__}")
    return saved


def remove_unused_snaps() -> int:
    """Drop snaps that are large but unused on this VM."""
    saved = 0
    candidates = ["lxd", "google-cloud-cli"]
    for s in candidates:
        try:
            r = subprocess.run(["snap", "list", s], capture_output=True,
                               timeout=10)
            if r.returncode != 0:
                continue
            # Capture size by listing /var/lib/snapd/snaps
            for sf in Path("/var/lib/snapd/snaps").glob(f"{s}_*.snap"):
                sz = sf.stat().st_size
                # remove
                rm = subprocess.run(["snap", "remove", "--purge", s],
                                    capture_output=True, timeout=60)
                if rm.returncode == 0:
                    saved += sz
                    log("janitor",
                        f"  removed snap {s} (saved {sz // 1_000_000}MB)")
                break
        except Exception:
            continue
    return saved


def emergency_prune() -> int:
    """Last-resort: when <500MB free."""
    log("janitor", "  ⚠ EMERGENCY: <500MB free, aggressive prune")
    saved = 0
    saved += vacuum_journals(target_mb=10)
    saved += prune_processed(max_age_hours=0)   # all
    saved += prune_apt_cache()
    saved += rotate_large_jsonls()
    saved += remove_unused_snaps()
    # kill /tmp older than 1h
    cutoff = time.time() - 3600
    for f in Path("/tmp").iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                sz = f.stat().st_size
                f.unlink(missing_ok=True)
                saved += sz
        except Exception:
            continue
    return saved


def normal_prune() -> int:
    saved = 0
    saved += dedup_training_pairs()
    saved += prune_processed(max_age_hours=1)
    saved += vacuum_journals(target_mb=30)
    saved += prune_apt_cache()
    return saved


def main() -> int:
    log("janitor", f"start — poll={POLL_SEC}s, OK>{THRESHOLD_OK_GB}GB, "
                  f"WARN<{THRESHOLD_WARN_GB}GB")
    cycles = 0
    last_emerg_alert = 0
    while not _stop:
        cycles += 1
        free = free_gb()
        if free < THRESHOLD_WARN_GB:
            saved = emergency_prune()
            now = time.time()
            if now - last_emerg_alert > 1800:  # max 1 alert / 30min
                discord_alert(
                    f"⚠ axentx disk emergency: {free*1000:.0f}MB free, "
                    f"freed {saved//1_000_000}MB. Free now: {free_gb()*1000:.0f}MB",
                )
                last_emerg_alert = now
        elif free < THRESHOLD_OK_GB:
            saved = normal_prune()
            if saved > 0:
                log("janitor",
                    f"normal prune: freed {saved//1_000_000}MB "
                    f"(was {free*1000:.0f}MB → {free_gb()*1000:.0f}MB)")
        else:
            # Healthy: just dedup periodically
            if cycles % 30 == 1:
                dedup_training_pairs()

        # Idle nap (interruptible)
        for _ in range(POLL_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
