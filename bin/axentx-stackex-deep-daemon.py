#!/usr/bin/env python3
"""axentx Stack Exchange deep-dive — pull from 12 SE sites with money-tag.

User direction 2026-05-10: 'ต่อๆ เอาเยอะๆ'

Stack Exchange = practitioners stuck in production. Each unanswered or
high-vote question = a paid SaaS opportunity.

Sites covered (12):
  serverfault, dba, stackoverflow, softwareengineering, security,
  devops, networkengineering, ux, workplace, freelancing,
  money, sysadmin (askubuntu), webmasters

For each site: pull top 50 questions tagged with high-money-intent tags
(e.g. aws, kubernetes, postgres, gdpr, hipaa, billing, monitoring).
"""
from __future__ import annotations
import datetime
import gzip
import hashlib
import html
import json
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, new_trace_id  # noqa: E402

CYCLE_GAP_SEC = float(os.environ.get("SE_CYCLE_GAP_SEC", "300"))
PER_REQ_GAP_SEC = float(os.environ.get("SE_REQ_GAP_SEC", "10.0"))
MAX_PER_SITE = int(os.environ.get("SE_MAX_PER_SITE", "8"))
MIN_TITLE_LEN = int(os.environ.get("SE_MIN_TITLE_LEN", "15"))

UA_POOL = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/120.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
]

CF_DEDUP_URL = os.environ.get(
    "CF_DEDUP_URL",
    "https://surrogate-1-cursor.ashira.workers.dev",
).rstrip("/")

_HOST = os.environ.get("HOSTNAME", "se-deep")
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("se-deep", "shutdown signal")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _ua():
    return random.choice(UA_POOL)


def _fp(url: str) -> str:
    return hashlib.md5(url.encode("utf-8", errors="replace")).hexdigest()[:16]


def _cf_seen_check(fps: list[str]) -> set[str] | None:
    if not (CF_DEDUP_URL and fps):
        return None
    try:
        req = urllib.request.Request(
            f"{CF_DEDUP_URL}/seen/check",
            data=json.dumps({"kind": "pain-url", "fps": fps[:200]}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": _ua()},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return set(json.loads(r.read()).get("unseen") or [])
    except Exception:
        return None


def _cf_seen_mark(fps: list[str]) -> None:
    if not (CF_DEDUP_URL and fps):
        return
    try:
        req = urllib.request.Request(
            f"{CF_DEDUP_URL}/seen/mark",
            data=json.dumps({
                "kind": "pain-url", "fps": fps[:200], "host": _HOST,
            }).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": _ua()},
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


# ── SE site config: (site, [tags], money_signal_score) ────────────────
# Score reflects how directly the site signals paying customer pain
SE_SITES = [
    # devops/cloud — paying customers stuck on AWS/k8s/db
    ("serverfault",     ["aws", "kubernetes", "monitoring", "high-availability",
                         "postgresql", "redis", "load-balancing"],   6),
    ("dba",             ["postgresql", "mysql", "performance", "replication",
                         "backup", "amazon-rds"],                    7),
    ("stackoverflow",   ["aws", "kubernetes", "stripe", "billing",
                         "rate-limiting", "production"],             5),
    ("softwareengineering", ["microservices", "api-design", "scalability",
                             "saas", "billing"],                     6),
    ("security",        ["compliance", "gdpr", "hipaa", "soc2", "pci-dss",
                         "audit"],                                   7),
    ("devops",          ["ci-cd", "monitoring", "infrastructure",
                         "logging", "observability"],                6),
    ("networkengineering", ["bgp", "vpn", "load-balancing", "monitoring",
                            "cisco", "fortinet"],                    6),
    # business / decision-maker stack
    ("workplace",       ["management", "compensation", "remote-work",
                         "productivity"],                            5),
    ("freelancing",     ["pricing", "contracts", "payment", "clients"], 7),
    ("money",           ["taxes", "small-business", "freelancing"],  5),
    ("ux",              ["pricing-page", "onboarding", "dashboards"], 4),
    ("webmasters",      ["seo", "google-analytics", "wordpress",
                         "ecommerce"],                               5),
]


def _se_api_questions(site_short: str, tag: str) -> list[dict]:
    """Use Stack Exchange API v2.3 — no auth required for public questions."""
    # Tag-filtered question listing, sorted by activity
    url = (
        f"https://api.stackexchange.com/2.3/questions?"
        f"order=desc&sort=activity&site={site_short}&tagged={tag}"
        f"&pagesize={MAX_PER_SITE}&filter=withbody"
    )
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _ua(),
            "Accept-Encoding": "gzip",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        d = json.loads(raw)
    except Exception as e:
        log("se-deep",
            f"  {site_short}:{tag} fetch fail: {type(e).__name__}: "
            f"{str(e)[:80]}")
        return []
    posts = []
    for q in (d.get("items") or [])[:MAX_PER_SITE]:
        title = html.unescape(q.get("title") or "").strip()
        if len(title) < MIN_TITLE_LEN:
            continue
        body_html = q.get("body") or ""
        body = re.sub(r"<[^>]+>", " ", body_html)
        body = html.unescape(body).strip()[:3000]
        url = q.get("link") or ""
        if not url:
            continue
        posts.append({
            "title": f"[SE:{site_short}/{tag}] {title}"[:500],
            "body": body[:6000],
            "url": url,
            "score": int(q.get("score") or 0),
            "is_answered": bool(q.get("is_answered", False)),
            "view_count": int(q.get("view_count") or 0),
            "source": f"se-deep:{site_short}:{tag}",
        })
    # Respect SE API rate limit (30 req/sec without key, 10K/day)
    return posts


def make_item(p: dict, site_score: int) -> dict:
    """Build pipeline item. SE questions with high views + unanswered = HIGH."""
    # Boost score if unanswered (someone wanted help, didn't get it)
    boost = 2 if not p.get("is_answered") else 0
    # Boost if many views (high-frequency pain)
    if p.get("view_count", 0) >= 1000:
        boost += 1
    final_score = min(site_score + boost, 10)
    sig = "high" if final_score >= 5 else "medium"
    item_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
        f"{p['source'].replace(':', '-')}-{_fp(p['url'])}"
    )
    return {
        "id": item_id,
        "trace_id": new_trace_id(),
        "discovery_id": item_id,
        "stage": "validator",
        "post": {
            "title": p["title"],
            "body": p.get("body", ""),
            "url": p["url"],
            "score": p.get("score", 0),
            "source": p["source"],
        },
        "monetary_signal": sig,
        "monetary_intent_score": final_score,
        "se_meta": {
            "is_answered": p.get("is_answered"),
            "view_count": p.get("view_count"),
        },
        "history": [{
            "stage": "se-deep",
            "actor": "se-deep",
            "output": f"emit (sig={sig}, score={final_score})",
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {
            "text": (f"[{p['source']}] {p['title']}\n\n"
                     f"{(p.get('body') or '')[:1500]}"),
        },
    }


def main() -> int:
    total_targets = sum(len(tags) for _, tags, _ in SE_SITES)
    log("se-deep",
        f"streaming SE deep across {len(SE_SITES)} sites × tags = "
        f"{total_targets} site-tag pairs (cycle={CYCLE_GAP_SEC}s)")

    while not _stop:
        cycle_start = time.time()
        emitted = 0
        skipped = 0
        for site, tags, site_score in SE_SITES:
            if _stop:
                break
            for tag in tags:
                if _stop:
                    break
                posts = _se_api_questions(site, tag)
                if not posts:
                    time.sleep(PER_REQ_GAP_SEC)
                    continue
                fps = [_fp(p["url"]) for p in posts]
                unseen = _cf_seen_check(fps)
                if unseen is None:
                    unseen = set(fps)
                mark_now = []
                for p, fp in zip(posts, fps):
                    if fp not in unseen:
                        skipped += 1
                        continue
                    item = make_item(p, site_score)
                    try:
                        write_item(item, "validator")
                        mark_now.append(fp)
                        emitted += 1
                        if emitted <= 5 or emitted % 10 == 0:
                            log("se-deep",
                                f"  ✓ {p['source']} score="
                                f"{item['monetary_intent_score']}/10: "
                                f"{p['title'][:60]}")
                    except Exception as e:
                        log("se-deep",
                            f"  ✗ write fail: {type(e).__name__}: "
                            f"{str(e)[:60]}")
                if mark_now:
                    _cf_seen_mark(mark_now)
                time.sleep(PER_REQ_GAP_SEC)
        elapsed = time.time() - cycle_start
        log("se-deep",
            f"cycle done — emitted={emitted}, skipped={skipped}, "
            f"elapsed={elapsed:.1f}s")
        remaining = max(0, CYCLE_GAP_SEC - elapsed)
        for _ in range(int(remaining)):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
