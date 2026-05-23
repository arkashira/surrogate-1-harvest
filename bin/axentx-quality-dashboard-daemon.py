#!/usr/bin/env python3
"""axentx Quality Dashboard — daily metrics summary across all tracks.

Writes /opt/surrogate-1-harvest/state/dashboard.json every 30 min:
  - commits_24h per repo + total
  - bd verdict mix (extend/paid/pass)
  - pitch GO rate
  - biz-plans count + heuristic vs LLM
  - PENDING tags per product
  - top trending categories from TRACK C
  - top monetary signals
  - validator throughput
  - LLM provider success rates
"""
from __future__ import annotations
import datetime
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

CYCLE_GAP_SEC = int(os.environ.get("DASHBOARD_CYCLE_SEC", "1800"))  # 30 min
OUTPUT_PATH = Path(os.environ.get(
    "DASHBOARD_OUTPUT_PATH",
    "/opt/surrogate-1-harvest/state/dashboard.json"))

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("quality-dashboard", "shutdown")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _sh(cmd):
    """Shell helper, returns stdout str."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=30)
        return r.stdout.strip()
    except Exception:
        return ""


def collect_metrics():
    """Build dashboard metrics dict."""
    m = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "host": os.environ.get("HOSTNAME", "unknown"),
    }

    # Git commits per axentx repo (24h)
    repos = []
    try:
        for d in sorted(Path("/opt/axentx").glob("*/")):
            if (d / ".git").is_dir():
                cnt = _sh(f"cd {d} && git log --since='24 hours ago' --oneline 2>/dev/null | wc -l")
                try:
                    n = int(cnt)
                except ValueError:
                    n = 0
                if n > 0:
                    repos.append({"name": d.name, "commits_24h": n})
    except Exception:
        pass
    m["commits_24h"] = sorted(repos, key=lambda x: -x["commits_24h"])
    m["commits_24h_total"] = sum(r["commits_24h"] for r in repos)

    # bd-daemon verdict mix (24h)
    bd_log = _sh("sudo journalctl -u 'axentx-bd-daemon*' --since '24 hours ago' "
                 "--no-pager 2>/dev/null | grep -oE 'mode=[a-z-]+' | sort | uniq -c")
    m["bd_verdicts_24h"] = bd_log

    # pitch GO rate (24h)
    pitch_go = _sh("sudo journalctl -u 'axentx-pitch-daemon*' --since '24 hours ago' "
                   "--no-pager 2>/dev/null | grep -cE 'extend GO|→ GO'")
    pitch_total = _sh("sudo journalctl -u 'axentx-pitch-daemon*' --since '24 hours ago' "
                      "--no-pager 2>/dev/null | grep -cE 'panel:|extend GO|extend NO-GO'")
    try:
        m["pitch_go_24h"] = int(pitch_go)
        m["pitch_total_24h"] = int(pitch_total)
        m["pitch_go_rate"] = (int(pitch_go) / max(int(pitch_total), 1) if pitch_total else 0)
    except ValueError:
        pass

    # biz-plans count
    try:
        biz = sorted(Path("/opt/axentx-biz").glob("*"))
        m["biz_plans_total"] = len(biz)
        m["biz_plans_heuristic"] = sum(1 for b in biz if "heuristic" in b.name)
    except Exception:
        m["biz_plans_total"] = 0

    # validator queue + throughput
    try:
        vq = Path("/opt/surrogate-1-harvest/state/swarm-shared/validator-queue")
        m["validator_queue"] = sum(1 for f in vq.iterdir()
                                   if f.suffix == ".json" and ".claimed-" not in f.name)
    except Exception:
        m["validator_queue"] = 0

    # PENDING tags from D1 portfolio
    try:
        ct = subprocess.check_output(
            ['bash', '-c', "grep '^CLOUDFLARE_API_TOKEN=' /etc/surrogate-coordinator.env | cut -d= -f2-"]
        ).decode().strip()
        import urllib.request
        req = urllib.request.Request(
            "https://api.cloudflare.com/client/v4/accounts/77fb5e6c3716be794dc3e8467ba9f285/d1/database/ae95ac58-7b7e-40d9-8708-518c23281ae6/query",
            data=json.dumps({"sql": "SELECT v FROM kv_store WHERE k=?",
                             "params": ["bd.portfolio"]}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {ct}",
                     "Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
        portfolio = json.loads(d["result"][0]["results"][0]["v"])
        pending_per_product = {
            slug: desc.count("PENDING-")
            for slug, desc in portfolio.get("products", {}).items()
            if desc.count("PENDING-") > 0
        }
        m["pending_tags"] = dict(sorted(pending_per_product.items(),
                                         key=lambda x: -x[1]))
        m["pending_total"] = sum(pending_per_product.values())
    except Exception as e:
        m["pending_error"] = str(e)[:80]

    return m


def main():
    log("quality-dashboard", f"start — cycle={CYCLE_GAP_SEC}s → {OUTPUT_PATH}")
    while not _stop:
        try:
            metrics = collect_metrics()
            OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT_PATH.write_text(json.dumps(metrics, indent=2,
                                               ensure_ascii=False))
            log("quality-dashboard",
                f"✓ wrote dashboard ({metrics.get('commits_24h_total', 0)} "
                f"commits, {metrics.get('pending_total', 0)} PENDING, "
                f"{metrics.get('biz_plans_total', 0)} biz plans)")
        except Exception as e:
            log("quality-dashboard",
                f"⚠ collect crashed: {type(e).__name__}: {str(e)[:100]}")
        for _ in range(CYCLE_GAP_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
