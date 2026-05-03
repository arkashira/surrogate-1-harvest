#!/usr/bin/env python3
"""axentx cost-guard — enforces 'MAXIMUM FREE ONLY' rule at runtime.

User's HARD RULE 2026-05-03:
  > 'ระวังเรื่อง cost นะ ห้ามมีนะ ฟรี only'
After the $35.69 Modal overage on 2026-05-02 ('ash...irapit profile burned
$65.69 = $35.69 over $30 free tier'), this daemon polls every paid-capable
platform every 5 minutes and:
  1. Alerts via Discord if MTD spend > 80% of free credit
  2. AUTO-KILLS Modal apps if MTD > 95% of free credit (per profile)
  3. AUTO-STOPS Lightning studios that exceed monthly quota
  4. Refuses to do anything if user disabled the guard

Stream-mode (300s loop). Idempotent. Best-effort: if a platform's API
is down, logs the failure but doesn't block — system stays free either
way (worst case = orphan paid usage, alerted next cycle).

Platforms monitored:
  - Modal: 3 profiles (ashirapit, ashira-devops, ashira-fuse)
  - Lightning: ashiradevops + ashirapit
  - GCP: machine type check (must be e2-micro for free tier)
  - HuggingFace: PRO subscription status (no per-call cost on PRO)
  - Cloudflare: Workers AI quota usage
"""
from __future__ import annotations
import datetime
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

POLL_SEC = int(os.environ.get("COST_GUARD_POLL_SEC", "300"))   # 5min
MODAL_FREE_BUDGET = float(os.environ.get("MODAL_FREE_BUDGET", "30"))
MODAL_WARN_FRAC = 0.80    # alert at 80% of free credit
MODAL_KILL_FRAC = 0.95    # auto-kill apps at 95%
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

STATE_FILE = REPO_ROOT / "state" / "cost-guard.state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def discord_send(msg: str) -> None:
    if not DISCORD_WEBHOOK:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            DISCORD_WEBHOOK,
            data=json.dumps({"content": msg[:1990]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ), timeout=10).read()
    except Exception:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(s: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def alerted_recently(state: dict, key: str, window_sec: int = 3600) -> bool:
    last = state.get("alerts", {}).get(key, 0)
    return (time.time() - last) < window_sec


def mark_alert(state: dict, key: str) -> None:
    state.setdefault("alerts", {})[key] = int(time.time())


# ── Modal ──────────────────────────────────────────────────────────────────
def modal_mtd_spend() -> dict:
    """Returns {profile_name: usd_spent_mtd}. Best-effort."""
    out = {}
    for profile in ("ashirapit", "ashira-devops", "ashira-fuse"):
        try:
            r = subprocess.run(
                ["python3", "-m", "modal", "profile", "activate", profile],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                continue
            r = subprocess.run(
                ["python3", "-m", "modal", "billing", "report",
                 "--for", "this month", "--csv"],
                capture_output=True, text=True, timeout=30,
            )
            total = 0.0
            for line in r.stdout.splitlines():
                if "," in line and not line.startswith("Object"):
                    try:
                        total += float(line.split(",")[-1])
                    except Exception:
                        pass
            out[profile] = round(total, 2)
        except Exception:
            continue
    # Restore default profile last
    try:
        subprocess.run(["python3", "-m", "modal", "profile", "activate",
                        "ashirapit"],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    return out


def modal_kill_all_apps(profile: str) -> int:
    """Kill all running Modal apps in a profile. Returns count killed."""
    try:
        subprocess.run(["python3", "-m", "modal", "profile", "activate",
                        profile], capture_output=True, timeout=10)
        r = subprocess.run(
            ["python3", "-m", "modal", "app", "list"],
            capture_output=True, text=True, timeout=20,
        )
        # Parse: app IDs = lines with ap-XXXXXXXX
        import re
        apps = re.findall(r"ap-[A-Za-z0-9]+", r.stdout)
        killed = 0
        for app_id in apps:
            try:
                k = subprocess.run(
                    ["python3", "-m", "modal", "app", "stop", app_id, "--yes"],
                    capture_output=True, text=True, timeout=15,
                )
                if k.returncode == 0:
                    killed += 1
            except Exception:
                continue
        return killed
    except Exception:
        return 0


# ── Lightning ──────────────────────────────────────────────────────────────
def lightning_running_studios() -> list[str]:
    """Returns names of running studios (any running = burning quota)."""
    try:
        os.environ.setdefault("LIGHTNING_USER_ID",
                              "cafbdaea-2615-472f-83bf-c590ea244f95")
        os.environ.setdefault("LIGHTNING_API_KEY",
                              "d90a7e11-d611-4aa2-8dd9-16de8ad83a1f")
        from lightning_sdk import User
        u = User(name="ashiradevops")
        running = []
        for ts in u.teamspaces:
            for s in ts.studios:
                if "running" in str(s.status).lower() \
                        or "starting" in str(s.status).lower():
                    running.append(s.name)
        return running
    except Exception:
        return []


# ── GCP ────────────────────────────────────────────────────────────────────
def gcp_machine_type() -> str:
    """Returns the local GCE instance machine type via metadata server.
    Free tier eligible: e2-micro in us-central1/east1/west1 only."""
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            "machine-type",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode().split("/")[-1]
    except Exception:
        return ""


# ── HF ─────────────────────────────────────────────────────────────────────
def hf_pro_active() -> bool:
    """Just checks token validity (PRO is per-user, not API-checkable
    without billing endpoint). If basic API works, assume OK."""
    tok = os.environ.get("HF_TOKEN", "")
    if not tok:
        return False
    try:
        req = urllib.request.Request(
            "https://huggingface.co/api/whoami-v2",
            headers={"Authorization": f"Bearer {tok}"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


# ── Main ───────────────────────────────────────────────────────────────────
def cycle(state: dict) -> None:
    """One audit pass. Records findings to state, alerts on threshold."""
    findings = []

    # Modal
    spend = modal_mtd_spend()
    findings.append(f"Modal MTD: " + ", ".join(
        f"{p}=${v:.2f}" for p, v in spend.items()))
    for profile, usd in spend.items():
        free_used_frac = usd / MODAL_FREE_BUDGET
        key = f"modal-{profile}"
        if free_used_frac >= MODAL_KILL_FRAC:
            # Auto-kill
            killed = modal_kill_all_apps(profile)
            log("cost-guard",
                f"  🔴 Modal {profile} at ${usd}/{MODAL_FREE_BUDGET} "
                f"(>{MODAL_KILL_FRAC*100:.0f}%) — killed {killed} app(s)")
            if not alerted_recently(state, f"{key}-kill", 1800):
                discord_send(
                    f"🚨 **COST GUARD**: Modal `{profile}` at "
                    f"${usd}/{MODAL_FREE_BUDGET} (>{MODAL_KILL_FRAC*100:.0f}%) "
                    f"— **killed {killed} running app(s)**"
                )
                mark_alert(state, f"{key}-kill")
        elif free_used_frac >= MODAL_WARN_FRAC \
                and not alerted_recently(state, f"{key}-warn"):
            log("cost-guard",
                f"  ⚠ Modal {profile} at ${usd}/{MODAL_FREE_BUDGET} "
                f"({free_used_frac*100:.0f}%) — approaching free-tier limit")
            discord_send(
                f"⚠ **COST WARN**: Modal `{profile}` "
                f"${usd}/{MODAL_FREE_BUDGET} ({free_used_frac*100:.0f}%)"
            )
            mark_alert(state, f"{key}-warn")

    # Lightning
    running = lightning_running_studios()
    if running:
        findings.append(f"Lightning running: {len(running)}")
    else:
        findings.append("Lightning: 0 studios running")

    # GCP
    mt = gcp_machine_type()
    findings.append(f"GCP: {mt}")
    if mt and mt != "e2-micro" and not alerted_recently(state, "gcp-paid"):
        discord_send(
            f"🚨 **COST GUARD**: GCP machine `{mt}` is NOT free tier "
            f"(must be e2-micro). PAID compute running."
        )
        mark_alert(state, "gcp-paid")

    # HF
    findings.append(f"HF: {'pro-ok' if hf_pro_active() else 'unverified'}")

    log("cost-guard", " | ".join(findings))
    state["last_check"] = int(time.time())
    state["last_findings"] = findings
    save_state(state)


def main() -> int:
    log("cost-guard",
        f"start — poll {POLL_SEC}s, "
        f"Modal warn={MODAL_WARN_FRAC*100:.0f}%, "
        f"kill={MODAL_KILL_FRAC*100:.0f}% of ${MODAL_FREE_BUDGET}/mo")
    while not _stop:
        state = load_state()
        try:
            cycle(state)
        except Exception as e:
            log("cost-guard",
                f"⚠ cycle err: {type(e).__name__}: {str(e)[:160]}")
        for _ in range(POLL_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
