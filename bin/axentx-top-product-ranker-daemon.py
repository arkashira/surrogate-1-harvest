#!/usr/bin/env python3
"""axentx Top-Product Ranker — score products by REAL DEMAND SIGNAL.

User direction 2026-05-11:
  > 'product ที่เป็นที่ต้องการ มากที่สุด live ได้เร็วที่สุด'

Multi-signal demand score per product (0-100):
  +30  EXTEND verdicts (pure market signal — bd LLM picked this product)
  +25  PENDING tags count (queued features = active development)
  +20  commits last 7 days (real shipping velocity)
  +10  biz-pipeline GO panels (panel said GO = revenue path validated)
  +10  pitch GO count (pre-spawn validation)
  +5   star-equivalent (size of repo on disk = depth of work done)

Output: /opt/surrogate-1-harvest/state/top-products.json (sorted)
Picked up by: landing-generator + cf-pages-deployer + marketing-burst

Cycle: 30 min. Provides ranking for downstream daemons to pick which
products to PRIORITIZE for go-live activities.
"""
from __future__ import annotations
import datetime
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

CYCLE_SEC = int(os.environ.get("RANKER_CYCLE_SEC", "1800"))
OUTPUT_PATH = Path("/opt/surrogate-1-harvest/state/top-products.json")
AXENTX_BASE = Path("/opt/axentx")

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("top-ranker", "shutdown")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _get_d1_portfolio():
    """Read bd.portfolio from D1."""
    try:
        ct = _sh("grep '^CLOUDFLARE_API_TOKEN=' /etc/surrogate-coordinator.env | cut -d= -f2-")
        req = urllib.request.Request(
            "https://api.cloudflare.com/client/v4/accounts/77fb5e6c3716be794dc3e8467ba9f285/d1/database/ae95ac58-7b7e-40d9-8708-518c23281ae6/query",
            data=json.dumps({"sql": "SELECT v FROM kv_store WHERE k=?",
                             "params": ["bd.portfolio"]}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {ct}",
                     "Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return json.loads(d["result"][0]["results"][0]["v"])
    except Exception as e:
        log("top-ranker", f"⚠ D1 portfolio fetch fail: {e}")
        return {"products": {}}


def _count_extend_verdicts(slug, hours=24):
    """Count EXTEND verdicts for a product in the last N hours."""
    try:
        cmd = (f"sudo journalctl -u 'axentx-bd-daemon*' --since '{hours} hours ago' "
               f"--no-pager 2>/dev/null | grep -c 'target={slug}'")
        return int(_sh(cmd) or 0)
    except Exception:
        return 0


def _count_pitch_go(slug, hours=24):
    """Count pitch GO for a product."""
    try:
        cmd = (f"sudo journalctl -u 'axentx-pitch-daemon*' --since '{hours} hours ago' "
               f"--no-pager 2>/dev/null | grep '{slug}' | grep -cE 'extend GO|→ GO'")
        return int(_sh(cmd) or 0)
    except Exception:
        return 0


def _count_biz_go(slug, hours=24):
    """Biz-pipeline GO that mentions this product."""
    try:
        cmd = (f"sudo journalctl -u 'axentx-biz-pipeline-daemon*' --since '{hours} hours ago' "
               f"--no-pager 2>/dev/null | grep '{slug}' | grep -cE '💼 GO'")
        return int(_sh(cmd) or 0)
    except Exception:
        return 0


def _count_commits(slug, days=7):
    """Recent commit velocity."""
    repo = AXENTX_BASE / slug
    if not (repo / ".git").is_dir():
        return 0
    out = _sh(
        f"cd {repo} && git log --since='{days} days ago' --oneline 2>/dev/null | wc -l"
    )
    try:
        return int(out)
    except ValueError:
        return 0


def _repo_size_mb(slug):
    """Repo working tree size MB (proxy for depth of work)."""
    repo = AXENTX_BASE / slug
    if not repo.is_dir():
        return 0.0
    out = _sh(f"du -sm {repo} 2>/dev/null | cut -f1")
    try:
        return int(out)
    except ValueError:
        return 0.0


def score_product(slug, desc):
    """Compute multi-signal demand score 0-100."""
    n_extend = _count_extend_verdicts(slug, hours=24)
    n_pending = desc.count("PENDING-")
    n_commits = _count_commits(slug, days=7)
    n_pitch_go = _count_pitch_go(slug, hours=24)
    n_biz_go = _count_biz_go(slug, hours=24)
    size_mb = _repo_size_mb(slug)

    # Weights (all out of 100):
    # Cap each component so single-channel dominance doesn't break ranking.
    s_extend = min(n_extend / 50 * 30, 30)       # 50 EXTEND/24h = max 30
    s_pending = min(n_pending / 20 * 25, 25)     # 20 PENDING = max 25
    s_commits = min(n_commits / 200 * 20, 20)    # 200/week = max 20
    s_biz = min(n_biz_go * 5, 10)                # 2 biz GO = max 10
    s_pitch = min(n_pitch_go / 10 * 10, 10)      # 10 pitch GO = max 10
    s_size = min(size_mb / 100 * 5, 5)           # 100MB = max 5
    total = round(s_extend + s_pending + s_commits + s_biz + s_pitch + s_size, 1)

    return {
        "slug": slug,
        "score": total,
        "signals": {
            "extend_24h": n_extend,
            "pending_features": n_pending,
            "commits_7d": n_commits,
            "biz_go_24h": n_biz_go,
            "pitch_go_24h": n_pitch_go,
            "size_mb": size_mb,
        },
        "category": _extract_category(desc),
        "buyer": _extract_buyer(desc),
        "tagline": _extract_tagline(desc),
    }


def _extract_category(desc):
    import re
    m = re.search(r"\[CATEGORY:\s*([\w-]+)\]", desc)
    return m.group(1) if m else "uncategorized"


def _extract_buyer(desc):
    import re
    m = re.search(r"BUYER:\s*([^·]+)", desc)
    return m.group(1).strip()[:120] if m else ""


def _extract_tagline(desc):
    """First sentence after [CATEGORY] block."""
    import re
    m = re.search(r"\]\s*(.+?)(?:\s*·|$)", desc)
    return m.group(1).strip()[:150] if m else desc[:150]


def main():
    log("top-ranker", f"start — cycle={CYCLE_SEC}s → {OUTPUT_PATH}")
    while not _stop:
        try:
            portfolio = _get_d1_portfolio()
            products = portfolio.get("products", {})
            ranked = []
            for slug, desc in products.items():
                if slug.startswith("PENDING-"):
                    continue
                score = score_product(slug, desc)
                ranked.append(score)
            ranked.sort(key=lambda x: -x["score"])

            output = {
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "total_products": len(ranked),
                "top_10": ranked[:10],
                "all_ranked": ranked,
                "live_priority": [p["slug"] for p in ranked[:5]
                                  if p["score"] >= 30],
            }
            OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT_PATH.write_text(json.dumps(output, indent=2,
                                               ensure_ascii=False))
            top3 = ", ".join(f"{p['slug']}({p['score']})" for p in ranked[:3])
            log("top-ranker",
                f"✓ ranked {len(ranked)} products. Top 3: {top3}")
        except Exception as e:
            log("top-ranker",
                f"⚠ rank cycle crashed: {type(e).__name__}: {str(e)[:120]}")
        for _ in range(CYCLE_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
