#!/usr/bin/env python3
"""axentx Discord notifier — stream pipeline events to Discord.

Watches Supabase pipeline_items + products-spawned.jsonl for state
changes since last cursor and emits formatted Discord webhook updates.
This is the "demo / update / FYI" channel — no human input requested,
just visibility into the agentic stream.

Events emitted:
  🆕 Product spawned: <slug>  (from products-spawned.jsonl)
  💡 Pain validated: <title> (research → bd transition)
  📐 Architecture drafted: <slug>  (architect stage exit)
  💼 BMC ready: <slug>  (business stage exit)
  🚢 Feature shipped: <slug>/<commit>  (commit-daemon ✓ pushed)
  ⚠ Stage stuck: <stage> (claimed >1h, no advance)

Cadence:
  - tight loop, 30s polls
  - per-event-type cooldown (don't spam same event repeatedly)
  - digest mode (>=10 events in window) collapses to 1 summary message
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

POLL_SEC = int(os.environ.get("NOTIFIER_POLL_SEC", "30"))
DIGEST_THRESHOLD = int(os.environ.get("NOTIFIER_DIGEST_THRESHOLD", "10"))
EVENT_DEDUP_SEC = int(os.environ.get("NOTIFIER_DEDUP_SEC", "300"))

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
SB_URL = os.environ.get(
    "SUPABASE_URL", "https://riunimyxoalicbntogbp.supabase.co",
).rstrip("/")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
SB_HEADERS = {
    "apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
    "Content-Type": "application/json",
}

CURSOR_FILE = REPO_ROOT / "state" / "swarm-shared" / "discord-notifier.cursor.json"
CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
SPAWNED_LOG = REPO_ROOT / "state" / "swarm-shared" / "products-spawned.jsonl"

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def load_cursor() -> dict:
    try:
        return json.loads(CURSOR_FILE.read_text())
    except Exception:
        return {"spawned_offset": 0, "last_commit_id": ""}


def save_cursor(c: dict) -> None:
    CURSOR_FILE.write_text(json.dumps(c, indent=2))


def discord_send(msg: str) -> bool:
    if not DISCORD_WEBHOOK or not msg:
        return False
    body = json.dumps({"content": msg[:1990]}).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "axentx-pipeline-notifier"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except urllib.error.HTTPError as e:
        if e.code == 429:
            log("notifier", "  ⚠ Discord 429 — wait 30s")
            time.sleep(30)
        return False
    except Exception as e:
        log("notifier", f"  send fail: {type(e).__name__}: {str(e)[:120]}")
        return False


def _sb(method: str, path: str):
    if not (SB_URL and SB_KEY):
        return None
    try:
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/{path}", method=method, headers=SB_HEADERS,
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ── Event sources ─────────────────────────────────────────────────────────
def new_spawned_products(cursor: dict) -> list[dict]:
    if not SPAWNED_LOG.exists():
        return []
    lines = SPAWNED_LOG.read_text().splitlines()
    offset = cursor.get("spawned_offset", 0)
    if offset >= len(lines):
        return []
    new = []
    for ln in lines[offset:]:
        try:
            new.append(json.loads(ln))
        except Exception:
            continue
    cursor["spawned_offset"] = len(lines)
    return new


def recent_commits(cursor: dict, since_seconds: int = 600) -> list[dict]:
    """pipeline_items where stage='done' and history contains commit ✓."""
    cutoff = int(time.time()) - since_seconds
    rows = _sb("GET",
               f"pipeline_items?stage=eq.done&updated_at=gte.{cutoff}"
               f"&order=updated_at.desc&select=id,project,payload"
               f"&limit=20")
    if not isinstance(rows, list):
        return []
    out = []
    last_seen = cursor.get("last_commit_id", "")
    for r in rows:
        if r["id"] == last_seen:
            break
        payload = r.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                continue
        # Only emit if last history entry was a successful commit
        hist = payload.get("history") or []
        if not hist:
            continue
        last = hist[-1]
        out_text = last.get("output", "")
        if "pushed to" not in out_text and "✓" not in out_text:
            continue
        out.append({
            "id": r["id"],
            "project": r.get("project") or payload.get("project"),
            "focus": payload.get("focus", ""),
            "history_tail": out_text[:200],
        })
    if rows:
        cursor["last_commit_id"] = rows[0]["id"]
    return out


def stuck_stages(threshold_min: int = 60) -> list[dict]:
    """pipeline_items claimed > threshold_min ago, still in same stage."""
    cutoff = int(time.time()) - (threshold_min * 60)
    rows = _sb("GET",
               f"pipeline_items?claimed_at=lt.{cutoff}"
               f"&claimed_at=not.is.null"
               f"&stage=neq.done&select=id,stage,project,claimed_by,claimed_at"
               f"&limit=10")
    return rows if isinstance(rows, list) else []


# ── Main loop ─────────────────────────────────────────────────────────────
def main() -> int:
    if not DISCORD_WEBHOOK:
        log("notifier", "⚠ DISCORD_WEBHOOK not set — events logged only")
    if not SB_KEY:
        log("notifier", "FATAL: SUPABASE_SECRET_KEY not set")
        return 1

    log("notifier", f"streaming pipeline events → Discord (poll={POLL_SEC}s)")
    cursor = load_cursor()
    last_stuck_alert = 0

    while not _stop:
        cycle_start = time.time()
        events = []

        # 1. New product spawns
        for s in new_spawned_products(cursor):
            slug = s.get("slug", "?")
            url = s.get("url", "")
            hyp = s.get("hypothesis", "")[:140]
            events.append(("spawn",
                           f"🆕 **New product spawned**: `{slug}`\n"
                           f"💡 {hyp}\n"
                           f"🔗 {url}"))

        # 2. Recent commits
        for c in recent_commits(cursor):
            events.append(("commit",
                           f"🚢 **Shipped**: {c.get('project','?')} "
                           f"`{c.get('focus','?')}` — {c.get('id','')[:32]}"))

        # 3. Stuck stages (rate-limit alert: at most 1/hour)
        if time.time() - last_stuck_alert > 3600:
            stuck = stuck_stages()
            if stuck:
                stuck_lines = []
                for s in stuck[:5]:
                    stuck_lines.append(
                        f"• `{s.get('stage','?')}` — "
                        f"`{s.get('id','')[:24]}` "
                        f"(claimed by `{s.get('claimed_by','?')}`)"
                    )
                events.append(("stuck",
                               "⚠ **Stages stuck >1h**:\n"
                               + "\n".join(stuck_lines)))
                last_stuck_alert = time.time()

        # Send (digest if many)
        if events:
            if len(events) >= DIGEST_THRESHOLD:
                summary = (
                    f"📊 **Pipeline digest** ({len(events)} events in last "
                    f"{POLL_SEC}s)\n"
                )
                by_kind: dict[str, int] = {}
                for kind, _ in events:
                    by_kind[kind] = by_kind.get(kind, 0) + 1
                for kind, n in sorted(by_kind.items()):
                    summary += f"  • {kind}: {n}\n"
                discord_send(summary)
            else:
                for _, msg in events:
                    discord_send(msg)
                    time.sleep(0.5)  # respect Discord rate-limit
            log("notifier", f"sent {len(events)} event(s)")

        save_cursor(cursor)
        # idle nap
        nap = max(0, POLL_SEC - (time.time() - cycle_start))
        for _ in range(int(nap)):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
