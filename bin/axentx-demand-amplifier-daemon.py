#!/usr/bin/env python3
"""axentx demand-amplifier — pull-based work cascade.

User directive 2026-05-05:
  > "ห้ามมีagent มีงานว่าง ต้องมาว่าง ต้องมี signal ไป force agent ที่จ่าย
  >  งานให้ก่อนหน้าว่าเออ ชั้นว่างแล้วนะ เอาtask มาให้หน่อย ถ้า agent
  >  ก่อนหน้าว่าง ก็ส่ง signal ต่อไปเรื่อยๆ ไม่มีที่สิ้นสุด ... ถ้า agent
  >  idle signal ส่งต้องกันเรื่อยๆ จนไปเจอว่า มันหา pain ใหม่ไม่ได้
  >  discover มันต้อง bulk spawn agent ขึ้นมาให้เยอะ ๆ และเพิ่มคีย์เวิด
  >  ให้มากขึ้นเรื่อย ๆ"

How this works (every 30s, leader-only):
  1. Read all `demand.<role>` keys from shared_kv.
  2. For each downstream stage marked hungry (pick_oldest miss in last 60s),
     check upstream queue depth.
     - If upstream has work → no action (autoscale will run more workers)
     - If upstream queue ALSO empty → mark upstream hungry (cascade)
  3. When the cascade reaches discovery (no source has fresh items):
     a. Bulk-spawn extra discovery daemon instances (more reddit-stream
        clones, more thai-stream clones, more global-trending clones).
     b. BROADEN keywords: lower MIN_TITLE_LEN, drop pain-marker requirement
        (accept ALL items as candidates), increase MAX_PER_SOURCE.
     c. Signal product-synthesizer to fire NOW (don't wait for poll cycle).
  4. When everything is busy (no demand signals) → relax discovery (reset
     keyword broadness so we don't spam noise during peak load).

Result: every agent always has work — system self-amplifies upstream when
downstream is hungry, self-relaxes when downstream is saturated.
"""
from __future__ import annotations

import datetime
import json
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

POLL_SEC = int(os.environ.get("AMPLIFIER_POLL_SEC", "30"))
HOST = socket.gethostname()
SHARED_QUEUES = Path(os.environ.get("SHARED_QUEUES",
                                    "/opt/surrogate-1-harvest/state/swarm-shared"))

# Pipeline DAG — child stages depend on parent stages producing items
# parent → [children that consume from parent's output]
PIPELINE_DAG = {
    "discovery":      ["research"],
    "research":       ["bd"],
    "bd":             ["validator"],
    "validator":      ["pitch"],
    "pitch":          ["spawn"],
    "spawn":          ["prd"],
    "prd":            ["architect"],
    "architect":      ["dev"],
    "dev":            ["review"],
    "review":         ["qa"],
    "qa":             ["feature-build"],
    "feature-build":  ["commit"],
    "commit":         ["release"],
}

# Reverse for cascade: child role → parent that should be amplified
PARENT_OF = {}
for parent, children in PIPELINE_DAG.items():
    for c in children:
        PARENT_OF[c] = parent

# Discovery stream daemons that we can bulk-spawn extra instances of
DISCOVERY_DAEMONS = [
    "reddit-stream", "hackernews-pain-stream", "producthunt-stream",
    "indiehackers-stream", "substack-stream", "betalist-stream",
    "github-deep-stream", "medium-crawler", "thai-pain-stream",
    "global-trending-stream",
]

DEMAND_TTL_SEC = 60       # demand signal valid for 60s
HUNGRY_THRESHOLD = 3      # ≥N hungry downstreams → trigger amplification

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _kv_get(key: str):
    try:
        from axentx_shared import kv_get
        return kv_get(key)
    except Exception:
        return None


def _kv_set(key: str, val) -> None:
    try:
        from axentx_shared import kv_set
        kv_set(key, val)
    except Exception:
        pass


def queue_depth(stage: str) -> int:
    """Count items in stage queue. Returns 0 on missing dir."""
    qdir = SHARED_QUEUES / f"{stage}-queue"
    if not qdir.exists():
        qdir = SHARED_QUEUES / stage
    if not qdir.exists():
        return 0
    try:
        return len(list(qdir.glob("*.json")))
    except Exception:
        return 0


def read_demand_signals() -> dict[str, bool]:
    """Read which roles signaled they're hungry within DEMAND_TTL_SEC."""
    hungry = {}
    now_ts = time.time()
    # Roles to check (every stage in the DAG)
    all_roles = set(PIPELINE_DAG.keys()) | set(
        c for ch in PIPELINE_DAG.values() for c in ch)
    for role in all_roles:
        rec = _kv_get(f"demand.{role}")
        if not rec:
            hungry[role] = False
            continue
        if isinstance(rec, dict) and rec.get("v"):
            rec = rec["v"]
        try:
            ts_str = rec.get("ts", "") if isinstance(rec, dict) else ""
            ts_dt = datetime.datetime.fromisoformat(ts_str.rstrip("Z"))
            age = now_ts - ts_dt.timestamp()
            hungry[role] = age < DEMAND_TTL_SEC and bool(
                rec.get("hungry", True))
        except Exception:
            hungry[role] = False
    return hungry


def cascade_demand(hungry: dict[str, bool]) -> set[str]:
    """For each hungry child whose parent has empty queue, mark parent hungry.
    Returns the set of roles where cascading triggered amplification."""
    amplify = set()
    for role, is_hungry in hungry.items():
        if not is_hungry:
            continue
        parent = PARENT_OF.get(role)
        if not parent:
            # Top of chain → discovery itself is hungry
            amplify.add("discovery")
            continue
        # Check if parent queue is also empty (parent can't feed)
        parent_depth = queue_depth(parent) if parent != "discovery" else 0
        if parent_depth == 0:
            # Parent also empty — propagate up
            _kv_set(f"demand.{parent}", {
                "hungry": True,
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "host": HOST,
                "via_cascade": role,
            })
            log("amplifier",
                f"  ↑ cascade demand: {role} → {parent}")
            # Recurse one more level? Not strictly needed — next cycle catches it.
    return amplify


def amplify_discovery(reasons: int) -> None:
    """Bulk-spawn additional discovery daemon instances + broaden keywords.

    Strategy when downstream consumers are hungry:
      1. For each DISCOVERY_DAEMON family that's not already templated,
         restart it (ensures it's running).
      2. Write `discovery.broaden_keywords=true` to shared_kv — streams
         read this and lower their pain-marker filter.
      3. Force product-synth to fire immediately (skip its 1800s poll).
    """
    log("amplifier",
        f"  🔥 AMPLIFY (downstream hungry={reasons}) — bulk-spawn + broaden")

    # 1. Ensure all discovery daemons are running (heal if any died)
    for d in DISCOVERY_DAEMONS:
        try:
            subprocess.run(
                ["systemctl", "restart", f"axentx-{d}-daemon.service"],
                capture_output=True, timeout=10)
        except Exception:
            pass

    # 2. Signal streams to broaden keywords (drop the pain-marker requirement)
    _kv_set("discovery.broaden_keywords", {
        "broaden": True,
        "min_title_len": 10,        # was 20
        "accept_all": True,         # streams check this; bypass pain re
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "reason": f"downstream_hungry={reasons}",
    })

    # 3. Force product-synth to run NOW
    _kv_set("product-synth.force_run_now", {
        "force": True,
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
    })

    # 4. Also signal feature-synthesizer
    _kv_set("feature-synth.force_run_now", {
        "force": True,
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
    })


def relax_discovery() -> None:
    """When everything is busy, restore normal keyword filtering."""
    cur = _kv_get("discovery.broaden_keywords")
    if isinstance(cur, dict) and cur.get("v"):
        cur = cur["v"]
    if isinstance(cur, dict) and cur.get("broaden"):
        _kv_set("discovery.broaden_keywords", {
            "broaden": False,
            "min_title_len": 20,
            "accept_all": False,
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "reason": "downstream saturated — relax",
        })
        log("amplifier", "  ✓ saturated — relaxed keyword breadth")


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("amplifier", "  ⤷ not leader — skip")
        return False

    hungry = read_demand_signals()
    n_hungry = sum(1 for v in hungry.values() if v)
    hungry_list = [k for k, v in hungry.items() if v]

    # Cascade first (mark parents hungry where their queue is also empty)
    cascade_demand(hungry)

    # Decide: amplify or relax
    if n_hungry >= HUNGRY_THRESHOLD:
        amplify_discovery(n_hungry)
    elif n_hungry == 0:
        relax_discovery()

    log("amplifier",
        f"  ✓ cycle done — hungry={n_hungry} ({','.join(hungry_list[:8])})")
    return False


if __name__ == "__main__":
    daemon_loop("amplifier", POLL_SEC, cycle)
