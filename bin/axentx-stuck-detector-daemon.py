#!/usr/bin/env python3
"""axentx stuck-detector — enforce 'no agent stuck' rule.

User directive 2026-05-05:
  > "ห้าม agent ทุกตัว ติดปัญหา ... ตั้ง rule ไว้เลย ห้ามมี agent ที่ที่
  >  ว่าง ในทุกตัว ทุก category"

Cycle (every 90s):
  1. Scan every queue dir under .shared/
  2. For each item, check mtime — items older than STUCK_THRESHOLD_SEC
     are stuck.
  3. Per-stage escalation policy:
     - dev/review/qa stuck > 30 min → bump dev_attempts to MAX → forces
       reviewer's escape hatch on next pull (degraded-APPROVE).
     - pitch stuck > 20 min → write degraded PIVOT verdict + advance.
     - feature-build stuck > 15 min → re-route to dev (re-spec).
     - bd/architect/prd stuck > 30 min → emit memory_log alert + skip.
  4. Items completely orphaned (no movement >2h) → archive to dead-letter
     so queues don't grow unbounded.
  5. Detect "agent never ran" — daemon active but processed=0 for >1h:
     emit agent-starved memory_log so user/orchestrator notice.
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

POLL_SEC = int(os.environ.get("STUCK_POLL_SEC", "90"))
HOST = socket.gethostname()
SHARED_QUEUES = Path(os.environ.get("SHARED_QUEUES",
                                    "/opt/surrogate-1-harvest/state/swarm-shared"))
DEAD_LETTER = SHARED_QUEUES / "_dead_letter"

# Per-stage stuck thresholds (seconds) + action
STAGE_POLICY = {
    "dev":              (1800, "force_max_attempt"),     # 30 min
    "review":           (1800, "force_max_attempt"),
    "qa":               (1800, "force_max_attempt"),
    "pitch":            (1200, "degraded_pivot"),         # 20 min
    "spawn":            (1200, "degraded_pivot"),
    "prd":              (1800, "alert"),
    "architect":        (1800, "alert"),
    "bd":               (1800, "alert"),
    "feature-build":    (900,  "reroute_dev"),            # 15 min
    "feature-synth":    (1800, "alert"),
    "commit":           (600,  "skip_or_retry"),          # 10 min
    "release":          (1800, "alert"),
}
ORPHAN_THRESHOLD_SEC = 7200   # 2h — archive to dead-letter

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _alert(role: str, item_id: str, reason: str) -> None:
    try:
        from axentx_shared import memory_log
        memory_log("stuck-detector", "stuck-item",
                   f"{role}: {item_id[:32]} stuck — {reason}",
                   tags=["stuck", role, "needs-attention"])
    except Exception:
        pass


def _force_max_attempt(item_path: Path) -> None:
    """Bump dev_attempts to MAX so reviewer's escape hatch fires."""
    try:
        item = json.loads(item_path.read_text())
        item["dev_attempts"] = max(int(item.get("dev_attempts", 1)), 3)
        item["override_reason"] = "stuck-detector force-bump"
        item_path.write_text(json.dumps(item, indent=2))
    except Exception:
        pass


def _degraded_pivot(item_path: Path) -> None:
    """Write a degraded PIVOT verdict + advance to next stage.

    2026-05-06: kill-switch added — if item ALREADY has
    pitch_verdict._degraded=True, this is the 2nd time it's stuck.
    Moving back to spawn-queue creates an infinite loop because spawner
    routes PIVOT items back to pitch, which is slow, hits stuck-detector
    again, etc. Instead, send to 'done' to break the cycle. Real items
    that get fresh pitch evaluations clear _degraded flag automatically.
    """
    try:
        item = json.loads(item_path.read_text())
        already_degraded = (item.get("pitch_verdict") or {}).get("_degraded") is True
        if already_degraded:
            # Already-degraded — kill the loop, move to done.
            target_dir = SHARED_QUEUES / "done"
            target_dir.mkdir(parents=True, exist_ok=True)
            new_path = target_dir / item_path.name
            item.setdefault("history", []).append({
                "stage": "stuck-detector",
                "actor": "stuck-detector",
                "output": "killed: already degraded — infinite loop prevention",
                "at": datetime.datetime.utcnow().isoformat() + "Z",
            })
            item_path.rename(new_path)
            new_path.write_text(json.dumps(item, indent=2))
            return
        # First-time degrade: PIVOT verdict + send to spawn for spawner gate
        item["pitch_verdict"] = {
            "verdict": "PIVOT",
            "rationale": "stuck-detector degraded-pivot (LLM unavailable >20min)",
            "_degraded": True,
        }
        target_dir = SHARED_QUEUES / "spawn-queue"
        target_dir.mkdir(parents=True, exist_ok=True)
        new_path = target_dir / item_path.name
        item_path.rename(new_path)
        new_path.write_text(json.dumps(item, indent=2))
    except Exception as e:
        log("stuck-detector", f"  ✗ pivot {item_path.name}: {type(e).__name__}")


def _reroute_dev(item_path: Path) -> None:
    """Move stuck feature-build item back to dev for re-spec."""
    try:
        item = json.loads(item_path.read_text())
        item["history"] = item.get("history", []) + [{
            "stage": "stuck-reroute",
            "actor": "stuck-detector",
            "output": "feature-build stuck > 15min → back to dev",
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }]
        target_dir = SHARED_QUEUES / "dev-queue"
        target_dir.mkdir(parents=True, exist_ok=True)
        new_path = target_dir / item_path.name
        item_path.rename(new_path)
        new_path.write_text(json.dumps(item, indent=2))
    except Exception:
        pass


def _archive_orphan(item_path: Path) -> None:
    DEAD_LETTER.mkdir(parents=True, exist_ok=True)
    try:
        item_path.rename(DEAD_LETTER / item_path.name)
    except Exception:
        pass


def scan_stage(stage_dir: Path, threshold_sec: int, action: str,
               role: str) -> dict:
    """Scan one stage queue. Returns counts {stuck, escalated, orphan}."""
    counts = {"stuck": 0, "escalated": 0, "orphan": 0}
    if not stage_dir.exists():
        return counts
    now = time.time()
    for item_path in stage_dir.glob("*.json"):
        try:
            mtime = item_path.stat().st_mtime
            age = now - mtime
        except Exception:
            continue
        if age > ORPHAN_THRESHOLD_SEC:
            _archive_orphan(item_path)
            counts["orphan"] += 1
            continue
        if age <= threshold_sec:
            continue
        counts["stuck"] += 1
        item_id = item_path.stem
        if action == "force_max_attempt":
            _force_max_attempt(item_path)
            counts["escalated"] += 1
        elif action == "degraded_pivot":
            _degraded_pivot(item_path)
            counts["escalated"] += 1
        elif action == "reroute_dev":
            _reroute_dev(item_path)
            counts["escalated"] += 1
        elif action in ("alert", "skip_or_retry"):
            _alert(role, item_id, f"age={int(age)}s")
            counts["escalated"] += 1
    return counts


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("stuck-detector", "  ⤷ not leader — skip")
        return False
    if not SHARED_QUEUES.exists():
        log("stuck-detector", "  ⤷ no shared queues dir — skip")
        return False

    grand = {"stuck": 0, "escalated": 0, "orphan": 0}
    for stage, (threshold, action) in STAGE_POLICY.items():
        stage_dir = SHARED_QUEUES / f"{stage}-queue"
        # Some stages have non-suffix names
        if not stage_dir.exists():
            stage_dir = SHARED_QUEUES / stage
        c = scan_stage(stage_dir, threshold, action, stage)
        for k in grand:
            grand[k] += c[k]
        if any(c.values()):
            log("stuck-detector",
                f"  ▸ {stage}: stuck={c['stuck']} "
                f"escalated={c['escalated']} orphan={c['orphan']}")
    log("stuck-detector",
        f"  ✓ cycle done — total stuck={grand['stuck']} "
        f"escalated={grand['escalated']} orphan={grand['orphan']}")
    return False


if __name__ == "__main__":
    daemon_loop("stuck-detector", POLL_SEC, cycle)
