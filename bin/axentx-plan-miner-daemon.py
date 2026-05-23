#!/usr/bin/env python3
"""axentx plan-miner — picks up unfinished .md plans inside each project's
`.axentx-dev-bot/`, `specs/`, `docs/plans/` directories and pushes them
to dev queue so they get implemented as REAL CODE.

User feedback 2026-05-04:
  > 'หลายโปรเจ็ค .md เป็นล้าน จะไม่มี plan ที่ยังไม่เสร็จหรือยังไม่ได้ทำ
  >  เลยเหรอ ... LLM-gate มี .axentx-dev-bot/ อยู่.md เป็นล้านเลย ทำไมไม่
  >  หยิบมาทำ ... ถึงบอก มันไม่ควรว่าง'

Strategy:
  1. For each /opt/axentx/<slug>/ repo, scan:
     - .axentx-dev-bot/*.md (output from past dev/feature-build cycles
       that contains plans/specs but no code committed yet)
     - specs/*.md (PRDs from prd-daemon)
     - docs/plans/*.md
  2. For each .md NOT yet mined (tracked via shared_kv["plan-mined.<slug>.<hash>"]):
     a. Read the plan content (first 2KB)
     b. Heuristic: if it looks like a TASK (E-S-T pattern, "implement", "add",
        "build", "create", "write the test for", etc.) → push to dev queue
     c. If just docs/notes with no actionable verb → skip (mark mined anyway)
  3. Mark mined immediately so we don't re-push.

Trigger: event-driven on dev queue depth. Same as feature-synth pattern —
fires when dev hungry, idle otherwise.
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
import signal
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("PLAN_MINER_POLL_SEC", "60"))
HOST = socket.gethostname()
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
DEV_QUEUE_TARGET = int(os.environ.get("DEV_QUEUE_TARGET", "30"))
MAX_PLANS_PER_CYCLE = int(os.environ.get("PLAN_MINER_MAX_PER_CYCLE", "20"))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _dev_queue_depth() -> int:
    if not (SB_URL and SB_KEY):
        return -1
    try:
        qs = urllib.parse.urlencode({"stage": "eq.dev", "select": "id"})
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Prefer": "count=exact", "Range": "0-0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            cr = r.headers.get("Content-Range", "")
            return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


def _is_mined(slug: str, plan_hash: str) -> bool:
    try:
        from axentx_shared import kv_get
        v = kv_get(f"plan-mined.{slug}.{plan_hash}")
        return bool(v)
    except Exception:
        return False


def _mark_mined(slug: str, plan_hash: str, plan_path: str,
                pushed_to_dev: bool) -> None:
    try:
        from axentx_shared import kv_set
        kv_set(f"plan-mined.{slug}.{plan_hash}", {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "path": plan_path,
            "pushed_to_dev": pushed_to_dev,
        })
    except Exception:
        pass


# Pattern: E<n>-E<n>-S<n>-T<n> = epic/story/task ID from prd output
_TASK_ID_RE = re.compile(r"E\d+-E\d+-S\d+-T\d+", re.IGNORECASE)
# Verbs that signal an actionable plan
_ACTIONABLE_RE = re.compile(
    r"\b(implement|add|build|create|write|extract|expose|integrate|"
    r"refactor|fix|wire|connect|migrate|deploy|generate|render|"
    r"emit|parse|validate|test|verify)\b", re.IGNORECASE)


def collect_plans(repo: Path) -> list[Path]:
    """All .md files in plan-bearing dirs."""
    candidates: list[Path] = []
    for sub in (".axentx-dev-bot", "specs", "docs/plans"):
        d = repo / sub
        if d.exists() and d.is_dir():
            candidates.extend(sorted(d.glob("*.md")))
    return candidates


def is_actionable(content: str) -> bool:
    """Return True if the .md describes work-to-be-done (vs just notes)."""
    head = content[:2500].lower()
    if _TASK_ID_RE.search(content[:200]):
        return True
    # Need ≥3 actionable verbs in the first 1KB
    return len(_ACTIONABLE_RE.findall(head[:1500])) >= 3


def push_plan_to_dev(slug: str, plan: Path, content: str) -> bool:
    """Insert pipeline_item at stage=dev so a dev daemon picks it up."""
    if not (SB_URL and SB_KEY):
        return False
    plan_hash = hashlib.md5(str(plan).encode()).hexdigest()[:10]
    fid = f"20260504-plan-{slug}-{plan_hash}"
    # Extract task identifier if present (E1-E1-S1-T1 etc) for traceability
    task_id_m = _TASK_ID_RE.search(content[:300])
    task_id = task_id_m.group(0) if task_id_m else "general"
    payload = {
        "id": fid,
        "stage": "dev",
        "project": slug,
        "focus": "plan-mined",
        "history": [{
            "stage": "plan-miner", "actor": "axentx-plan-miner",
            "output": f"mined plan: {plan.name} (task={task_id})",
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {"text": content[:6000]},
        "spec_source": str(plan),
        "task_id": task_id,
        "auto_mined": True,
    }
    body = {
        "id": fid, "stage": "dev",
        "project": slug, "focus": "plan-mined",
        "payload": payload,
    }
    try:
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        log("plan-miner", f"  ✗ push {fid}: {e}")
        return False


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("plan-miner", "  ⤷ not leader — skip")
        return False

    depth = _dev_queue_depth()
    if depth < 0:
        return False
    if depth >= DEV_QUEUE_TARGET:
        log("plan-miner",
            f"  ⤷ dev queue {depth} ≥ target {DEV_QUEUE_TARGET} — wait")
        return False

    if not PROJECTS_ROOT.exists():
        return False

    pushed = 0
    skipped = 0
    for repo in sorted(PROJECTS_ROOT.iterdir()):
        if not repo.is_dir() or not (repo / ".git").exists():
            continue
        plans = collect_plans(repo)
        if not plans:
            continue
        slug = repo.name
        for plan in plans:
            plan_hash = hashlib.md5(str(plan).encode()).hexdigest()[:10]
            if _is_mined(slug, plan_hash):
                continue
            try:
                content = plan.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if not is_actionable(content):
                # mark mined anyway to avoid re-checking
                _mark_mined(slug, plan_hash, str(plan), pushed_to_dev=False)
                skipped += 1
                continue
            if push_plan_to_dev(slug, plan, content):
                _mark_mined(slug, plan_hash, str(plan), pushed_to_dev=True)
                pushed += 1
                log("plan-miner",
                    f"  ✓ {slug}/{plan.name} → dev queue")
                if pushed >= MAX_PLANS_PER_CYCLE:
                    break
        if pushed >= MAX_PLANS_PER_CYCLE:
            break

    log("plan-miner",
        f"  ✓ pushed {pushed} plans (skipped {skipped} non-actionable)")
    if pushed:
        try:
            from axentx_shared import memory_log
            memory_log("plan-miner", "mined-plans",
                       f"pushed {pushed} pre-existing plans to dev",
                       body=f"dev_depth_before={depth}",
                       tags=["plan-miner", HOST])
        except Exception:
            pass
    return False


if __name__ == "__main__":
    daemon_loop("plan-miner", POLL_SEC, cycle)
