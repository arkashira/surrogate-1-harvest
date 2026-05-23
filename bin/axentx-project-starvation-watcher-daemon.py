#!/usr/bin/env python3
"""axentx project-starvation-watcher — per-project work-pipeline keeper.

User feedback 2026-05-04:
  > 'ถ้า project ไหนไม่เหลือ feature หรืออะไรให้ dev ต่อแล้ว ต้องให้
  >  research ไปหามาต่อ ให้ feature synthesis agent ทำงาน หา pain หา
  >  อะไรมาเพิ่ม เพื่อให้มี ฟีเจอร์มาเติมเรื่อยๆ ทุกตัว'

Cycle (60s tick, leader=GCP):
  1. Read shared_kv["bd.portfolio"] → list of slugs
  2. Count pipeline_items per project across (design, architect, ux, prd,
     tech-lead, dev, review) — work that's "in flight" for that project
  3. If a project has < THRESHOLD items in flight → flag as starving
  4. For each starving project:
     a. Read project-truth from shared_knowledge to get domain keywords
     b. Push 3 events to drive feed:
        - feature-synth.priority.<slug> = bump priority for that slug
        - source-discover.targeted_keywords = inject project keywords
        - pipeline-feeder.priority.<slug> = bump
  5. shared_kv["project-starvation"] = current state for visibility
"""
from __future__ import annotations
import datetime
import json
import os
import signal
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from collections import Counter

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, daemon_loop,  # noqa: E402
                             get_portfolio)

POLL_SEC = int(os.environ.get("STARVATION_WATCHER_POLL_SEC", "60"))
HOST = socket.gethostname()
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
STARVATION_THRESHOLD = int(os.environ.get("STARVATION_THRESHOLD", "5"))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def fetch_in_flight() -> dict[str, int]:
    """Count active pipeline items per project across in-flight stages."""
    if not (SB_URL and SB_KEY):
        return {}
    in_flight_stages = ("design", "architect", "ux", "prd", "tech-lead",
                        "dev", "review", "qa", "commit")
    counts: Counter = Counter()
    for stage in in_flight_stages:
        try:
            qs = urllib.parse.urlencode({
                "stage": f"eq.{stage}",
                "select": "project", "limit": "1500",
            })
            req = urllib.request.Request(
                f"{SB_URL}/rest/v1/pipeline_items?{qs}",
                headers={"apikey": SB_KEY,
                         "Authorization": f"Bearer {SB_KEY}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                rows = json.loads(r.read())
            for row in rows:
                p = row.get("project") or "(none)"
                if p and p != "(none)":
                    counts[p] += 1
        except Exception as e:
            log("starvation-watcher", f"  ⚠ fetch {stage}: {e}")
    return dict(counts)


def fetch_project_truth_keywords(slug: str) -> list[str]:
    """Pull project-truth → category, audience keywords for source-discover hint."""
    if not (SB_URL and SB_KEY):
        return []
    try:
        qs = urllib.parse.urlencode({
            "slug": f"eq.project-truth/{slug}",
            "select": "body,metadata",
        })
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/shared_knowledge?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            rows = json.loads(r.read())
        if not rows:
            return []
        meta = rows[0].get("metadata") or {}
        body = rows[0].get("body") or ""
        try:
            truth = json.loads(body)
        except Exception:
            truth = {}
        kw = []
        if meta.get("category"):
            kw.append(meta["category"])
        if truth.get("category"):
            kw.append(truth["category"])
        if truth.get("audience_actual"):
            kw.append(truth["audience_actual"])
        # Tech stack as keywords too
        for t in (truth.get("tech_stack") or []):
            if t and len(t) < 25:
                kw.append(t)
        return list(set(k for k in kw if k))[:8]
    except Exception:
        return []


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("starvation-watcher", "  ⤷ not leader — skip")
        return False

    portfolio = get_portfolio()
    if not portfolio:
        return False
    in_flight = fetch_in_flight()

    starving = []
    healthy = []
    for slug in portfolio:
        if slug in {"arkship", "cost-radar"}:
            continue
        depth = in_flight.get(slug, 0)
        if depth < STARVATION_THRESHOLD:
            starving.append((slug, depth))
        else:
            healthy.append((slug, depth))

    log("starvation-watcher",
        f"  starving={len(starving)} healthy={len(healthy)} "
        f"(threshold={STARVATION_THRESHOLD})")

    # Push priority hints for starving projects so feature-synth +
    # pipeline-feeder + source-discoverer process them FIRST next cycle.
    if starving:
        try:
            from axentx_shared import kv_set, memory_log
            priority_payload = {}
            for slug, depth in starving:
                kw = fetch_project_truth_keywords(slug)
                priority_payload[slug] = {
                    "depth": depth, "keywords": kw,
                    "since": datetime.datetime.utcnow().isoformat() + "Z",
                }
            kv_set("project-starvation", {
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "host": HOST,
                "threshold": STARVATION_THRESHOLD,
                "starving": priority_payload,
                "healthy": dict(healthy),
                "n_starving": len(starving),
                "n_healthy": len(healthy),
            })
            memory_log("starvation-watcher", "starvation-detected",
                       f"{len(starving)} projects starving",
                       body=json.dumps(priority_payload, indent=2)[:1500],
                       tags=["starvation-watcher", HOST])
            for slug, _ in starving[:5]:
                log("starvation-watcher",
                    f"  ⚠ {slug}: depth={in_flight.get(slug,0)} "
                    f"(target={STARVATION_THRESHOLD})")
        except Exception as e:
            log("starvation-watcher", f"  ⚠ kv_set: {e}")

    return False


if __name__ == "__main__":
    daemon_loop("starvation-watcher", POLL_SEC, cycle)
