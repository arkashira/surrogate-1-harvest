#!/usr/bin/env python3
"""axentx task-clarifier — bridges dev's 'need_clarification' signals back
to spec-writing agents (prd / architect / design-thinking) so dev never
has to invent plans.

User feedback 2026-05-04:
  > 'plan ต้องเขียนจริง แต่ไม่ใช่ dev มาเขียน — dev ลง code. plan ให้
  >  agent อื่นเขียน หา agent มาเขียนเลย แล้ว dev ทำ ......'

How it works:
  1. dev-daemon receives a vague task → emits a ```clarify {...}``` block
     instead of code (per new DEV_SYSTEM 2026-05-04).
  2. The item goes to review-queue with that block.
  3. THIS daemon scans review-queue for clarify blocks. When found:
     a. Read the request_to field (prd|architect|design-thinking)
     b. Re-route the item to that stage with feedback="<minimal_spec_needed>"
     c. The receiving daemon refines the spec, advances back to dev with
        proper file paths + acceptance criteria.
  4. dev gets a concrete spec next cycle → ships code.

Cycle: 60s tick (cheap — only acts when clarify blocks present in review).
Leader=GCP (single-host owns reroute to avoid double-bounce).
"""
from __future__ import annotations
import datetime
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

POLL_SEC = int(os.environ.get("TASK_CLARIFIER_POLL_SEC", "60"))
HOST = socket.gethostname()
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


_CLARIFY_RE = re.compile(
    r"```clarify\s*\n(\{[^`]*?\})\s*\n```", re.DOTALL | re.IGNORECASE)


def find_clarify_block(text: str) -> dict | None:
    m = _CLARIFY_RE.search(text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def fetch_review_with_clarify(limit: int = 30) -> list[dict]:
    """Pull review-stage items + scan their current.text for clarify blocks."""
    if not (SB_URL and SB_KEY):
        return []
    try:
        qs = urllib.parse.urlencode({
            "stage": "eq.review",
            "select": "id,project,focus,payload,claimed_by,created_at",
            "order": "created_at.desc",
            "limit": str(limit),
        })
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except Exception as e:
        log("task-clarifier", f"  ⚠ fetch: {e}")
        return []


def reroute_item(item_id: str, target_stage: str,
                 reason: str, minimal_spec: str) -> bool:
    if not (SB_URL and SB_KEY):
        return False
    try:
        # Update pipeline_item: stage=<target>, append history, set
        # clarification_request so the target daemon knows what to refine.
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items?id=eq.{item_id}",
            data=json.dumps({
                "stage": target_stage,
                "claimed_by": None,
                "claimed_at": None,
            }).encode(),
            method="PATCH",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        urllib.request.urlopen(req, timeout=10).read()
        # Also drop a hint into shared_memory so target daemon sees context
        try:
            from axentx_shared import memory_log
            memory_log("task-clarifier", "rerouted",
                       f"{item_id[:32]} → {target_stage} (clarify)",
                       body=(f"reason: {reason}\n"
                             f"minimal_spec_needed: {minimal_spec}\n"
                             f"requested_by: dev"),
                       tags=["task-clarifier", target_stage])
        except Exception:
            pass
        return True
    except Exception as e:
        log("task-clarifier", f"  ✗ reroute {item_id}: {e}")
        return False


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("task-clarifier", "  ⤷ not leader — skip")
        return False

    items = fetch_review_with_clarify(limit=30)
    if not items:
        return False

    rerouted = 0
    for x in items:
        payload = x.get("payload") or {}
        if isinstance(payload, str):
            try: payload = json.loads(payload)
            except: payload = {}
        text = (payload.get("current") or {}).get("text", "")
        if not text:
            continue
        clarify = find_clarify_block(text)
        if not clarify or not clarify.get("need_clarification"):
            continue
        target = (clarify.get("request_to") or "prd").strip().replace(
            "-daemon", "")
        # Map allowed targets to canonical stage names
        target_map = {
            "prd": "prd", "architect": "architect",
            "design-thinking": "design", "design": "design",
            "ux": "ux", "pm": "prd",   # pm doesn't have queue, route to prd
        }
        target_stage = target_map.get(target, "prd")
        reason = (clarify.get("reason") or "")[:300]
        minimal = (clarify.get("minimal_spec_needed") or "")[:400]
        if reroute_item(x["id"], target_stage, reason, minimal):
            rerouted += 1
            log("task-clarifier",
                f"  ↺ {x['id'][:32]} → {target_stage} "
                f"(reason: {reason[:60]})")

    if rerouted:
        log("task-clarifier",
            f"  ✓ rerouted {rerouted} clarify-request(s) to spec agents")
    return False


if __name__ == "__main__":
    daemon_loop("task-clarifier", POLL_SEC, cycle)
