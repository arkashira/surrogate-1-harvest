#!/usr/bin/env python3
"""axentx incident responder — auto-fix common failure patterns.

User's directive (2026-05-02): "เห็น noti ที่มันเฟลเนี่ย ได้มี agent ไปคอย
monitor ไหมว่ามันมีปัญหาหรือเปล่า มันต้องมีนะคอยแก้ให้มันใช้ได้เสมอ"
("there must be an agent watching and keeping things working")

What it auto-fixes:

  1. GitHub Actions failures (axentx repos)
     - Polls /repos/{owner}/{repo}/actions/runs?status=failure&per_page=10
     - For each NEW failure (not seen before): rerun once via
       POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs
     - State file tracks (repo,run_id) → attempts so we never rerun > MAX_RERUNS
     - On 2nd failure of same run, escalate to Discord (no more reruns)

  2. Render deploy stuck/failed
     - If RENDER_API_KEY is set, polls /v1/services then /v1/services/{id}/deploys
     - For deploys in update_failed/canceled/build_failed/deactivated state,
       trigger a fresh deploy via POST /v1/services/{id}/deploys.
     - Skipped silently if RENDER_API_KEY not set.

What it does NOT touch:
  - HF Spaces hung — handled by oci-watchdog-daemon (auto-restart via HF API)
  - systemd daemons — handled by oci-self-heal-daemon
  - Pipeline failures from non-axentx repos — out of scope

Discord posts only on:
  - Successful auto-recovery ("✓ rerun successful")
  - Escalation ("🚨 N failed reruns, manual intervention needed")
NOT on transient failures.

Idempotent + persistent — restart-safe via state/.incident-responder.json.
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
STATE_FILE = REPO_ROOT / "state" / ".incident-responder.json"
LOG_FILE = REPO_ROOT / "logs" / "incident-responder.log"

POLL_SEC = int(os.environ.get("INCIDENT_POLL_SEC", "300"))  # 5 min
MAX_RERUNS = int(os.environ.get("INCIDENT_MAX_RERUNS", "1"))

GH_TOKEN = os.environ.get("AXENTX_BOT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
GH_ORG = os.environ.get("AXENTX_GH_ORG", "axentx")
GH_REPOS = os.environ.get(
    "AXENTX_GH_REPOS",
    "Costinel,vanguard,airship,workio,surrogate-1",
).split(",")

RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
DISCORD = os.environ.get("DISCORD_WEBHOOK", "")

UA_BROWSER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"gh_runs": {}, "render_deploys": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"gh_runs": {}, "render_deploys": {}}


def save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, indent=2))


def post_discord(msg: str) -> None:
    if not DISCORD:
        return
    body = json.dumps({"content": msg[:1900]}).encode()
    req = urllib.request.Request(DISCORD, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/arkashira/surrogate-1-harvest, 1.0)",
    })
    try:
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:
        log(f"  discord post failed: {e}")


def gh_request(method: str, path: str, body=None) -> tuple[int, dict | list | None]:
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": UA_BROWSER,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = json.dumps(body).encode() if body else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            content = r.read()
            parsed = json.loads(content) if content else None
            return r.status, parsed
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
        except Exception:
            err_body = None
        return e.code, err_body
    except Exception as e:
        log(f"  gh fail {method} {path}: {e}")
        return 0, None


def sweep_github() -> int:
    """Find recent failed runs, rerun once. Returns # actions taken."""
    if not GH_TOKEN:
        return 0
    state = load_state()
    n_actions = 0

    for repo in GH_REPOS:
        repo = repo.strip()
        if not repo:
            continue
        # Pull last 10 failed runs per repo
        status, runs = gh_request(
            "GET",
            f"/repos/{GH_ORG}/{repo}/actions/runs?status=failure&per_page=10",
        )
        if status != 200 or not isinstance(runs, dict):
            continue
        for run in (runs.get("workflow_runs") or [])[:10]:
            run_id = run.get("id")
            if not run_id:
                continue
            key = f"{GH_ORG}/{repo}#{run_id}"
            entry = state["gh_runs"].setdefault(
                key, {"attempts": 0, "first_seen": datetime.datetime.utcnow().isoformat() + "Z"}
            )
            if entry["attempts"] >= MAX_RERUNS:
                continue
            # Skip runs older than 6h — too stale to be worth rerunning
            try:
                created = datetime.datetime.fromisoformat(
                    run["created_at"].replace("Z", "+00:00")
                )
                age_h = (datetime.datetime.now(created.tzinfo) - created).total_seconds() / 3600
                if age_h > 6:
                    continue
            except Exception:
                pass

            log(f"▸ rerun {key} (attempt {entry['attempts'] + 1}/{MAX_RERUNS})")
            rc, _ = gh_request(
                "POST",
                f"/repos/{GH_ORG}/{repo}/actions/runs/{run_id}/rerun-failed-jobs",
            )
            entry["attempts"] += 1
            entry["last_attempt"] = datetime.datetime.utcnow().isoformat() + "Z"
            entry["last_status"] = rc
            n_actions += 1
            if rc in (201, 204):
                log(f"  ✓ rerun queued for {key}")
                post_discord(
                    f"🔧 incident-responder: re-ran failed workflow `{repo}` "
                    f"(run {run_id}, attempt {entry['attempts']}/{MAX_RERUNS})"
                )
            else:
                log(f"  ✗ rerun failed: HTTP {rc}")
                if entry["attempts"] >= MAX_RERUNS:
                    post_discord(
                        f"🚨 incident-responder: `{repo}` workflow {run_id} "
                        f"still failing after {MAX_RERUNS} rerun(s) — manual fix needed"
                    )
            time.sleep(2.0)  # gentle on GH API

    save_state(state)
    return n_actions


def render_request(method: str, path: str, body=None) -> tuple[int, dict | list | None]:
    url = f"https://api.render.com{path}"
    headers = {
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Accept": "application/json",
        "User-Agent": UA_BROWSER,
    }
    data = json.dumps(body).encode() if body else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        log(f"  render fail {method} {path}: {e}")
        return 0, None


FAILED_DEPLOY_STATES = {
    "update_failed", "build_failed", "canceled",
    "deactivated", "pre_deploy_failed",
}


def sweep_render() -> int:
    """Trigger fresh deploy for any service whose latest deploy failed."""
    if not RENDER_API_KEY:
        return 0
    state = load_state()
    n_actions = 0

    rc, services = render_request("GET", "/v1/services?limit=50")
    if rc != 200 or not services:
        return 0

    for entry in services:
        svc = entry.get("service") if isinstance(entry, dict) else None
        if not svc or not svc.get("id"):
            continue
        sid = svc["id"]
        rc, deploys = render_request("GET", f"/v1/services/{sid}/deploys?limit=1")
        if rc != 200 or not deploys:
            continue
        latest_w = deploys[0] if isinstance(deploys, list) else None
        latest = latest_w.get("deploy") if isinstance(latest_w, dict) else None
        if not latest:
            continue
        st = latest.get("status", "")
        deploy_id = latest.get("id", "")
        key = f"{sid}#{deploy_id}"
        if st not in FAILED_DEPLOY_STATES:
            # Reset tracker — service recovered on its own
            state["render_deploys"].pop(key, None)
            continue
        rec = state["render_deploys"].setdefault(
            key, {"attempts": 0, "first_seen": datetime.datetime.utcnow().isoformat() + "Z"}
        )
        if rec["attempts"] >= MAX_RERUNS:
            continue
        log(f"▸ render redeploy {svc.get('name','?')} (status={st})")
        rc, _ = render_request("POST", f"/v1/services/{sid}/deploys", {"clearCache": "do_not_clear"})
        rec["attempts"] += 1
        rec["last_attempt"] = datetime.datetime.utcnow().isoformat() + "Z"
        n_actions += 1
        if rc in (201, 200):
            log(f"  ✓ render redeploy triggered for {svc.get('name','?')}")
            post_discord(
                f"🔧 incident-responder: triggered redeploy on Render "
                f"`{svc.get('name','?')}` (was {st})"
            )
        else:
            log(f"  ✗ render redeploy failed: HTTP {rc}")
            if rec["attempts"] >= MAX_RERUNS:
                post_discord(
                    f"🚨 incident-responder: Render `{svc.get('name','?')}` "
                    f"redeploy failed (HTTP {rc}) — manual fix needed"
                )
        time.sleep(2.0)

    save_state(state)
    return n_actions


def shutdown(*_):
    log("shutdown")
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def main() -> int:
    log(f"start — poll every {POLL_SEC}s, GH={'on' if GH_TOKEN else 'OFF'}, "
        f"Render={'on' if RENDER_API_KEY else 'OFF'}")
    n = 0
    while True:
        n += 1
        try:
            gh = sweep_github()
            rd = sweep_render()
            if gh or rd:
                log(f"sweep #{n}: gh_actions={gh}, render_actions={rd}")
            elif n % 12 == 1:
                log(f"sweep #{n}: nothing to fix")
        except Exception as e:
            log(f"⚠ sweep error: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
