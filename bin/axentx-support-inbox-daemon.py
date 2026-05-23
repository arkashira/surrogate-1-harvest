#!/usr/bin/env python3
"""Customer-support inbox → Discord daemon.

Polls a webhook URL (set via SUPPORT_INBOX_URL env) for new tickets and
forwards each one to the Discord support channel via DISCORD_WEBHOOK.

Polling is preferred over receiving SMTP because:
  - We do not own MX records yet (axentx.com email routing TBD)
  - Cloudflare Email Routing can forward to a worker that exposes a
    JSON endpoint we can poll
  - This keeps the daemon stateless + restart-safe with one tiny seen-id
    file at $REPO_ROOT/state/.support-inbox-seen.json

Polled endpoint contract (set up later via CF Email Routing → Worker):
  GET {SUPPORT_INBOX_URL}?since=<ISO8601>
    → 200 {"tickets":[{"id","received_at","from","subject","body"}, ...]}
  Auth via X-Auth-Token from SUPPORT_INBOX_TOKEN.

Until Email Routing is wired, the daemon happily polls a placeholder
URL and exits cleanly when the response is empty (no tickets) or 404
(endpoint not deployed yet).

Env:
  SUPPORT_INBOX_URL          (default https://axentx-support.workers.dev/inbox)
  SUPPORT_INBOX_TOKEN        (optional bearer for the Worker)
  DISCORD_WEBHOOK            (required to actually post)
  SUPPORT_POLL_INTERVAL_SEC  (default 60)
  REPO_ROOT
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
LOG_FILE = REPO_ROOT / "logs" / "axentx-support-inbox-daemon.log"
SEEN_FILE = REPO_ROOT / "state" / ".support-inbox-seen.json"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)

INBOX_URL = os.environ.get("SUPPORT_INBOX_URL", "https://axentx-support.workers.dev/inbox").rstrip("/")
INBOX_TOKEN = os.environ.get("SUPPORT_INBOX_TOKEN", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
POLL_INTERVAL = int(os.environ.get("SUPPORT_POLL_INTERVAL_SEC", "60"))
TIMEOUT_SEC = int(os.environ.get("SUPPORT_POLL_TIMEOUT_SEC", "20"))
MAX_BODY_CHARS = 1500  # Discord caps content at 2000 chars

_running = True


def log(msg: str) -> None:
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] [support-inbox] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {"ids": [], "last_poll_at": None}
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception:
        return {"ids": [], "last_poll_at": None}


def save_seen(state: dict) -> None:
    # Cap retained ids at 1k so the file stays small.
    if len(state.get("ids", [])) > 1000:
        state["ids"] = state["ids"][-1000:]
    SEEN_FILE.write_text(json.dumps(state))


def fetch_tickets(since: str | None) -> list[dict]:
    qs = f"?since={since}" if since else ""
    url = f"{INBOX_URL}{qs}"
    headers = {"User-Agent": "axentx-support-inbox/1.0"}
    if INBOX_TOKEN:
        headers["X-Auth-Token"] = INBOX_TOKEN
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log("inbox endpoint 404 — Email Routing not deployed yet, skipping")
            return []
        log(f"inbox HTTP {exc.code}: {exc.reason}")
        return []
    except (urllib.error.URLError, TimeoutError) as exc:
        log(f"inbox unreachable: {exc}")
        return []
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        log(f"inbox returned non-JSON ({payload[:120]})")
        return []
    return data.get("tickets") or []


def post_ticket(ticket: dict) -> bool:
    if not DISCORD_WEBHOOK:
        log(f"(no webhook; would post ticket {ticket.get('id')})")
        return True
    body = (ticket.get("body") or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + " …"
    embed = {
        "title": (ticket.get("subject") or "(no subject)")[:256],
        "description": body or "(empty body)",
        "color": 0x5865F2,
        "fields": [
            {"name": "From", "value": (ticket.get("from") or "?")[:1024], "inline": True},
            {"name": "Received", "value": (ticket.get("received_at") or "?")[:1024], "inline": True},
            {"name": "Ticket ID", "value": f"`{ticket.get('id', '?')}`", "inline": True},
        ],
        "footer": {"text": "axentx support inbox"},
    }
    payload = {"embeds": [embed], "content": ":envelope: new support ticket"}
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except Exception as exc:
        log(f"discord post failed for ticket {ticket.get('id')}: {exc}")
        return False


def main() -> int:
    log(f"START url={INBOX_URL} interval={POLL_INTERVAL}s")
    if not DISCORD_WEBHOOK:
        log("WARN DISCORD_WEBHOOK missing — tickets will be logged only")

    def stop(_sig, _frame):
        global _running
        _running = False
        log("shutdown signal — exiting after current poll")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    state = load_seen()
    seen_ids: set[str] = set(state.get("ids", []))

    while _running:
        try:
            tickets = fetch_tickets(state.get("last_poll_at"))
            new_count = 0
            for t in tickets:
                tid = str(t.get("id", "")).strip()
                if not tid or tid in seen_ids:
                    continue
                if post_ticket(t):
                    seen_ids.add(tid)
                    state["ids"] = list(seen_ids)
                    new_count += 1
            state["last_poll_at"] = datetime.datetime.utcnow().isoformat() + "Z"
            save_seen(state)
            if new_count:
                log(f"posted {new_count} new ticket(s) (seen total={len(seen_ids)})")
        except Exception as exc:
            log(f"poll loop error: {exc}")

        slept = 0
        while _running and slept < POLL_INTERVAL:
            time.sleep(1)
            slept += 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
