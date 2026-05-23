#!/usr/bin/env python3
"""axentx kamatera-terminator — runs hourly on GCP. Terminates the Kamatera
VM at day 28 of the 1MONTH300 promo (promo expires day 30, we kill at 28
for safety). Replaces the Mac-side LaunchAgent so the kill switch survives
laptop sleep/shutdown.

Why Python + REST instead of cloudcli: cloudcli ships pre-built binaries
only for darwin-arm64 (the Mac source). Cross-compiling to linux-amd64
adds Go toolchain + ~50 MB. The REST API is 4 endpoints, way simpler.

Discord warnings on day 25 + day 27. Idempotent: writes terminated_at
into the state file once destroyed; later runs no-op.

Env required:
  KAM_CLIENT_ID, KAM_SECRET     Kamatera API credentials
  STATE_FILE                    path to kamatera-server.json (default
                                /opt/surrogate-1-harvest/state/
                                kamatera-server.json)
  DISCORD_WEBHOOK               for warnings (optional)
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

KAM_API = "https://cloudcli.cloudwm.com"  # same endpoint cloudcli hits
KAM_CLIENT_ID = os.environ.get("KAM_CLIENT_ID", "")
KAM_SECRET = os.environ.get("KAM_SECRET", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

STATE_FILE = Path(os.environ.get(
    "STATE_FILE",
    "/opt/surrogate-1-harvest/state/kamatera-server.json",
))
WARN_STAMP_DIR = Path(os.environ.get(
    "WARN_STAMP_DIR",
    "/opt/surrogate-1-harvest/state/.kamatera-warn-stamps",
))
LOG_PREFIX = "[kamatera-terminator]"


def log(msg: str) -> None:
    ts = datetime.datetime.utcnow().strftime("%H:%M:%SZ")
    print(f"{LOG_PREFIX} {ts} {msg}", flush=True)


def kam_request(method: str, path: str, body: dict | None = None) -> dict:
    """POST to Kamatera REST API with the documented header auth."""
    url = f"{KAM_API}{path}"
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "AuthClientId": KAM_CLIENT_ID,
        "AuthSecret": KAM_SECRET,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def post_discord(message: str) -> None:
    if not DISCORD_WEBHOOK:
        return
    body = json.dumps({"content": message}).encode()
    req = urllib.request.Request(DISCORD_WEBHOOK, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log(f"  discord post failed: {e}")


def warn_once(day: int, message: str) -> None:
    WARN_STAMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = WARN_STAMP_DIR / f"day{day}"
    if stamp.exists():
        return
    log(f"▸ warning day {day}")
    post_discord(message)
    stamp.touch()


def terminate_vm(server_id: str, name: str) -> bool:
    log(f"▸ terminating {name} ({server_id})")
    try:
        resp = kam_request("POST", "/service/server/terminate", {
            "id": server_id,
            "force": True,
        })
        log(f"  resp: {json.dumps(resp)[:200]}")
        return True
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", errors="replace")
        log(f"  ✗ HTTP {e.code}: {body}")
        post_discord(f"🚨 **Kamatera auto-terminate FAILED** "
                     f"— {name} still running. MANUAL: "
                     f"console.kamatera.com → terminate {server_id}. "
                     f"HTTP {e.code}: {body[:150]}")
        return False
    except Exception as e:
        log(f"  ✗ {type(e).__name__}: {e}")
        return False


def mark_terminated() -> None:
    d = json.loads(STATE_FILE.read_text())
    d["terminated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    STATE_FILE.write_text(json.dumps(d, indent=2))


def main() -> int:
    if not (KAM_CLIENT_ID and KAM_SECRET):
        log("missing KAM_CLIENT_ID / KAM_SECRET — skipping")
        return 0
    if not STATE_FILE.exists():
        log("no kamatera-server.json — nothing provisioned yet")
        return 0
    state = json.loads(STATE_FILE.read_text())
    if state.get("terminated_at"):
        log(f"already terminated at {state['terminated_at']} — exit")
        return 0

    server_id = state["server_id"]
    name = state.get("name", "")
    created = state["created_at"]
    created_dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    age = datetime.datetime.now(datetime.timezone.utc) - created_dt
    age_days = age.days

    log(f"server {name} ({server_id}): age={age_days} days")

    if age_days == 25:
        warn_once(25, f"⏰ **Kamatera promo day 25** — auto-terminate in 3 days. "
                       f"Server `{name}` ({server_id}). Free promo expires day 30; "
                       f"we kill at 28 for safety.")
    elif age_days == 27:
        warn_once(27, f"⚠️ **Kamatera promo day 27** — TERMINATING TOMORROW. "
                       f"Server `{name}`. Pull anything you need now.")
    elif 28 <= age_days <= 31:
        warn_once(28, f"🛑 **Kamatera day {age_days}** — AUTO-TERMINATING NOW")
        if terminate_vm(server_id, name):
            mark_terminated()
            post_discord(f"✅ **Kamatera auto-terminate** — `{name}` terminated "
                         f"at day {age_days} (promo expires day 30, $0 charge).")
    elif age_days > 31:
        log("✗✗ over day 31 and STILL RUNNING — emergency")
        post_discord(f"🆘 **EMERGENCY** Kamatera `{name}` age={age_days}d. "
                     f"Promo EXPIRED. Manual terminate via console.kamatera.com NOW.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
