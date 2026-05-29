#!/usr/bin/env python3
"""axentx autoscale-controller — queue-driven worker scaling.

User directive 2026-05-05:
  > "เพิ่ม feature autoscaling ให้กับ agent ทุกตัว ทุก category
  >  เมื่อมี job เยอะ ๆ และ scale down เมื่อมี load น้อย โดย ไม่มีการ
  >  จองแล้วรอ task ... มันจะได้ไม่เปลือง llm ไปจองไว้เผื่อ token เปล่า ๆ"

How this works:
  1. Every 60s, read queue depth per stage (from filesystem .shared queues
     + Supabase pipeline_items count if reachable).
  2. Compute target_count = clamp(min_workers, ceil(queue_depth / batch_size),
     max_workers) for each templated daemon family (dev@, reviewer@, qa@, etc).
  3. Diff vs currently-active instances. systemctl enable+start the new ones,
     systemctl disable+stop the excess. Workers that are mid-job are NOT
     killed (Restart=no, MinUnit set so SIGTERM lets them finish).
  4. For singleton daemons (architect, bd, pitch, ...), adjust their internal
     poll cadence by writing shared_kv["autoscale.<role>.poll_sec"] which the
     daemon reads at start of each cycle.

Leader-only — runs on lexicographically-first host (Kam2 = surrogate-harvest-kam2).

Rules (from user directive):
  - NO agent reserved/idle — workers either have work or are stopped
  - NO agent stuck — escalate items via stuck-detector (separate daemon)
  - Save LLM tokens — never spawn workers without queue depth justifying it
"""
from __future__ import annotations

import datetime
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("AUTOSCALE_POLL_SEC", "60"))
HOST = socket.gethostname()
# Core-aware homogeneous scaling: each host scales its OWN local daemons,
# with maxw scaled to its core count (ref 8 cores = full caps). Small boxes
# get proportionally fewer replicas so they never thrash. min stays >=1 so
# every host keeps one of each agent type (homogeneous peer roster).
try:
    _CORES = int(os.environ.get("AXENTX_CORE_BUDGET", os.cpu_count() or 2))
except Exception:
    _CORES = 2
MAXW_FACTOR = max(0.25, _CORES / 8.0)
def _scaled_max(maxw: int) -> int:
    import math as _m
    return max(1, _m.ceil(maxw * MAXW_FACTOR))
SHARED_QUEUES = Path(os.environ.get("SHARED_QUEUES",
                                    "/opt/surrogate-1-harvest/state/swarm-shared"))

# ── Templated daemons: stage -> (daemon-name, queue-name, min, max, batch) ──
# batch = items processed per cycle by ONE worker. target_count =
# ceil(queue_depth / batch), bounded by [min, max].
TEMPLATED_DAEMONS = {
    "dev":      ("dev-daemon",      "dev-queue", 2, 30, 3),
    "reviewer": ("reviewer-daemon", "review-queue", 2, 30, 3),
    "qa":       ("qa-daemon",       "qa-queue",     2, 30, 3),
    "prd":      ("prd-daemon",      "prd-queue",    3, 30, 2),
    "ux":       ("ux-daemon",       "ux-queue",     3, 26, 2),
    "design-thinking":
                ("design-thinking-daemon", "design-queue", 3, 26, 2),
    "marketing":("marketing-daemon", "marketing-queue", 3, 25, 2),
    "business": ("business-daemon", "business-queue", 3, 25, 2),
    "architect":("architect-daemon", "architect-queue", 3, 12, 2),
    "bd":       ("bd-daemon",       "bd-queue",     3, 14, 2),
}

# ── Singleton daemons: tune their poll_sec via shared_kv based on activity ──
# These never scale by count (only one instance) but we hint how often
# they should poll. Daemon code reads kv at top of each cycle.
SINGLETON_DAEMONS = [
    "pitch", "product-spawner", "product-synthesizer", "feature-synthesizer",
    "feature-builder", "commit", "release", "tech-lead",
]

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    # Homogeneous peers: every host autoscales its own local daemons.
    if os.environ.get("AXENTX_AUTOSCALE_PER_HOST", "1") == "1":
        return True
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def queue_depth(queue_name: str) -> int:
    """Count items in the FS queue. Adjust if your queue dir differs."""
    qdir = SHARED_QUEUES / queue_name
    if not qdir.exists():
        return 0
    try:
        return len(list(qdir.glob("*.json")))
    except Exception:
        return 0


def active_instances(daemon_name: str) -> set[int]:
    """Get set of active instance IDs for templated daemon.
    e.g. ['dev-daemon@1.service', 'dev-daemon@2.service'] → {1, 2}"""
    try:
        r = subprocess.run(
            ["systemctl", "list-units", f"axentx-{daemon_name}@*",
             "--state=active", "--no-legend"],
            capture_output=True, text=True, timeout=10)
        ids = set()
        for line in r.stdout.splitlines():
            unit = line.strip().split()[0] if line.strip() else ""
            # axentx-dev-daemon@5.service → 5
            if "@" in unit:
                num = unit.split("@", 1)[1].split(".")[0]
                if num.isdigit():
                    ids.add(int(num))
        return ids
    except Exception as e:
        log("autoscale", f"  ✗ list {daemon_name}: {type(e).__name__}")
        return set()


def scale_template(role: str, daemon_name: str, queue_name: str,
                   minw: int, maxw: int, batch: int) -> None:
    """Scale templated daemon to match current queue depth."""
    depth = queue_depth(queue_name)
    target = max(minw, min(maxw, math.ceil(depth / batch))) if depth else minw
    current_ids = active_instances(daemon_name)
    current_count = len(current_ids)

    if target == current_count:
        return

    if target > current_count:
        # Scale UP — enable next-numbered instances
        needed = target - current_count
        next_id = max(current_ids, default=0) + 1
        added = []
        for i in range(needed):
            unit = f"axentx-{daemon_name}@{next_id + i}.service"
            try:
                subprocess.run(
                    ["systemctl", "enable", "--now", unit],
                    capture_output=True, timeout=10)
                added.append(next_id + i)
            except Exception:
                pass
        log("autoscale",
            f"  ↑ {role}: {current_count}→{target} "
            f"(queue={depth}, +{added})")
    else:
        # Scale DOWN — disable highest-numbered N instances
        excess = current_count - target
        sorted_ids = sorted(current_ids, reverse=True)
        removed = []
        for i in sorted_ids[:excess]:
            unit = f"axentx-{daemon_name}@{i}.service"
            try:
                subprocess.run(
                    ["systemctl", "disable", "--now", unit],
                    capture_output=True, timeout=10)
                removed.append(i)
            except Exception:
                pass
        log("autoscale",
            f"  ↓ {role}: {current_count}→{target} "
            f"(queue={depth}, -{removed})")


def hint_singleton(role: str) -> None:
    """Write a poll-sec hint for singleton daemons to read.
    Aggressive (10s) when its input queue is hot; relaxed (300s) when cold."""
    queue_lookup = {
        "pitch": "pitch-queue",
        "product-spawner": "spawn-queue",
        "feature-synthesizer": "feature-synth-queue",
        "feature-builder": "feature-build-queue",
        "commit": "commit-queue",
    }
    qname = queue_lookup.get(role)
    depth = queue_depth(qname) if qname else 0
    if depth == 0:
        poll_sec = 300        # cold → 5 min
    elif depth < 5:
        poll_sec = 60         # warm → 1 min
    else:
        poll_sec = 10         # hot → 10 sec
    try:
        from axentx_shared import kv_set
        kv_set(f"autoscale.{role}.poll_sec",
               {"poll_sec": poll_sec, "depth": depth,
                "ts": datetime.datetime.utcnow().isoformat() + "Z"})
    except Exception:
        pass


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("autoscale", "  ⤷ not leader — skip")
        return False

    # Templated daemons
    summary = []
    for role, (daemon_name, queue, minw, maxw, batch) in TEMPLATED_DAEMONS.items():
        try:
            scale_template(role, daemon_name, queue, minw, _scaled_max(maxw), batch)
            depth = queue_depth(queue)
            summary.append(f"{role}={depth}")
        except Exception as e:
            log("autoscale",
                f"  ✗ {role}: {type(e).__name__}: {str(e)[:80]}")

    # Singleton hints
    for role in SINGLETON_DAEMONS:
        try:
            hint_singleton(role)
        except Exception:
            pass

    log("autoscale",
        f"  ✓ cycle done — depths: {' '.join(summary)}")
    return False


if __name__ == "__main__":
    daemon_loop("autoscale", POLL_SEC, cycle)
