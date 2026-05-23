#!/usr/bin/env python3
"""axentx env-sync — keeps /etc/surrogate-coordinator.env in sync across
all hosts via shared_kv["env.canonical"]. Mac-independent — once the
canonical blob is in Supabase, every host self-heals env drift.

User feedback 2026-05-04:
  > 'มันต้องไม่มีอะไรมาพึ่ง mac แล้วค่าา'

Flow:
  1. Read shared_kv["env.canonical"] — dict of {key: value} pairs.
  2. Parse local /etc/surrogate-coordinator.env into the same shape.
  3. For each canonical key:
     - missing locally → append
     - present but different → update (canonical wins)
  4. NEVER delete local-only keys (host-specific overrides like
     DAEMON_SOFT_RSS_KB stay).
  5. On change → restart all axentx-*-daemon services (graceful).
  6. memory_log "env-synced" with diff summary so peers see what changed.

Bootstrapping (one-time, run via Bash on any host):
  curl ... -X POST .../shared_kv?... -d '{"k":"env.canonical","v":{...}}'
  After that, Mac is no longer needed for env management.

Leader election:
  Every host runs this daemon. It only writes to local /etc — no race.
  Reads are cached for 5min. Writes only on diff.
"""
from __future__ import annotations
import datetime
import os
import re
import signal
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("ENV_SYNC_POLL_SEC", "300"))   # 5 min
ENV_FILE = Path(os.environ.get("ENV_FILE", "/etc/surrogate-coordinator.env"))
HOST = socket.gethostname()

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _sh(cmd: list[str], t: int = 30, input_data: str | None = None
        ) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=t,
                           input=input_data)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


_KEY_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _KEY_RE.match(line)
            if m:
                out[m.group(1)] = m.group(2)
    except Exception as e:
        log("env-sync", f"  ⚠ parse fail: {e}")
    return out


def _serialize_env(items: dict[str, str], header: str = "") -> str:
    lines = []
    if header:
        lines.append(f"# {header}")
    for k in sorted(items):
        lines.append(f"{k}={items[k]}")
    return "\n".join(lines) + "\n"


def cycle():
    if _stop:
        return False
    try:
        from axentx_shared import kv_get, memory_log
    except Exception:
        log("env-sync", "  ⚠ axentx_shared unavailable — skip cycle")
        return False

    canonical = kv_get("env.canonical")
    if not canonical or not isinstance(canonical, dict):
        log("env-sync", "  ⤷ shared_kv['env.canonical'] not set yet — "
                       "nothing to sync (bootstrap pending)")
        return False
    if "v" in canonical and isinstance(canonical["v"], dict):
        canonical = canonical["v"]
    items_canonical = canonical.get("items") or canonical
    if not isinstance(items_canonical, dict):
        log("env-sync", "  ⚠ canonical shape unrecognized — abort")
        return False

    local = _parse_env_file(ENV_FILE)
    added: list[str] = []
    updated: list[str] = []
    for k, v in items_canonical.items():
        v = str(v)
        if k not in local:
            added.append(k)
            local[k] = v
        elif local[k] != v:
            updated.append(k)
            local[k] = v

    if not added and not updated:
        log("env-sync", f"  ✓ env in sync ({len(local)} keys, no drift)")
        return False

    # Write merged env file (atomic via tee — preserves ownership)
    new_content = _serialize_env(
        local,
        header=(f"axentx coordinator env — synced {datetime.datetime.utcnow()}"
                f" by env-sync-daemon@{HOST} from shared_kv[env.canonical]"))
    rc, _, err = _sh(["sudo", "tee", str(ENV_FILE)],
                     t=10, input_data=new_content)
    if rc != 0:
        log("env-sync", f"  ✗ write failed: {err[:120]}")
        return False
    _sh(["sudo", "chmod", "0644", str(ENV_FILE)], t=5)

    summary = f"+{len(added)} new, {len(updated)} updated"
    log("env-sync", f"  ✓ env-sync applied: {summary}")
    if added:
        log("env-sync", f"    new: {', '.join(sorted(added)[:8])}")
    if updated:
        log("env-sync", f"    updated: {', '.join(sorted(updated)[:8])}")

    # Restart all axentx-* daemons so they pick up new env. Skip ourselves.
    rc, out, _ = _sh(
        ["systemctl", "list-units", "--type=service", "--state=active",
         "axentx-*", "--no-legend"], t=10)
    units = [l.split()[0] for l in (out or "").strip().split("\n")
             if l.split() and "axentx-env-sync-daemon" not in l]
    if units:
        # Batch restart — systemctl handles it
        _sh(["sudo", "systemctl", "restart"] + units, t=60)
        log("env-sync", f"  ↻ restarted {len(units)} daemons "
                       f"to pick up new env")

    try:
        memory_log("env-sync", "env-synced",
                   f"env diff applied on {HOST}: {summary}",
                   body=(f"Added: {', '.join(sorted(added))}\n"
                         f"Updated: {', '.join(sorted(updated))}\n"
                         f"Total keys after: {len(local)}\n"
                         f"Daemons restarted: {len(units)}"),
                   tags=["env-sync", HOST])
    except Exception:
        pass
    return False


if __name__ == "__main__":
    daemon_loop("env-sync", POLL_SEC, cycle)
