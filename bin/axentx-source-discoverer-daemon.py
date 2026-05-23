#!/usr/bin/env python3
"""axentx source-discoverer — finds NEW pain sources beyond existing
streams, multi-language, stamps visited so we don't re-mine.

User feedback 2026-05-04:
  > 'discovery streams ตัน — แก้สิ หาsource เพิ่ม หรือ deep search ไปอีก
  >  ไปที่ไหนแล้ว stamp ไว้ แล้วหาที่ใหม่ โดย base จาก keyword โลกนี้มี
  >  source ไม่รู้เท่าไหร่ ไม่รู้กี่ประเทศกี่ภาษา'

Strategy:
  1. Mine recent pain keywords from shared_memory + bd verdicts (last 24h)
  2. For each keyword: search across MULTIPLE source types:
     - GitHub topics + repos (pain in issues)
     - Reddit subreddit search (TH + EN + community-named subs)
     - HackerNews Algolia search
     - DEV.to / Lobsters / Pantip / Bahasa-equivalents
     - StackExchange network search (multilingual)
     - Indie Hackers / Product Hunt search
  3. Stamp every discovered source in shared_kv["discovery.visited"]
     so we never re-mine the same URL.
  4. Push new sources discovered into shared_kv["discovery.new_sources"]
     with metadata (kind, lang, recency, est_pain_density).
  5. Existing per-source daemons (reddit-stream, medium-crawler, etc.)
     read from this list to extend their territory.

Cycle: every 1 hour. Leader=GCP. ~50 keyword × 6 sources ≈ 300 queries
per cycle, each returning ~10 candidates → dedup via shared_kv stamps.
"""



from __future__ import annotations

# 2026-05-15 excepthook + tb-on-exception
import sys as _sd_sys
import traceback as _sd_tb
def _sd_excepthook(et, ev, tb):
    out = ''.join(_sd_tb.format_exception(et, ev, tb))[-1500:]
    try:
        from axentx_pipeline import log
        log('source-discover', 'UNCAUGHT ' + et.__name__ + ': ' + str(ev) + ' | TB: ' + out)
    except Exception:
        print('[source-discover] UNCAUGHT:', out, file=_sd_sys.stderr)
_sd_sys.excepthook = _sd_excepthook
import datetime
import hashlib
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

# Tick — fires only when research/validator stages stale (no recent advances).
POLL_SEC = int(os.environ.get("SOURCE_DISCOVER_POLL_SEC", "300"))   # 5-min tick
HOST = socket.gethostname()
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _get(url: str, headers: dict | None = None,
         timeout: int = 15) -> bytes | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "axentx-source-discoverer", **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _is_visited(url: str) -> bool:
    try:
        from axentx_shared import kv_get
        v = kv_get(f"discovery.visited.{hashlib.md5(url.encode()).hexdigest()[:12]}")
        return bool(v)
    except Exception:
        return False


def _mark_visited(url: str, kind: str, lang: str = "?") -> None:
    try:
        from axentx_shared import kv_set
        h = hashlib.md5(url.encode()).hexdigest()[:12]
        kv_set(f"discovery.visited.{h}", {
            "url": url, "kind": kind, "lang": lang,
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
        })
    except Exception:
        pass


def fetch_recent_keywords() -> list[str]:
    """Mine last 24h shared_memory + recent bd PASS rationales for keywords."""
    if not (SB_URL and SB_KEY):
        return []
    cutoff = (datetime.datetime.utcnow()
              - datetime.timedelta(hours=24)).isoformat()
    keywords: dict[str, int] = {}
    try:
        # shared_memory
        qs = urllib.parse.urlencode({
            "created_at": f"gte.{cutoff}",
            "select": "title,body,kind", "limit": "200",
        })
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/shared_memory?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read())
        for x in rows:
            txt = (x.get("title", "") + " " + (x.get("body") or "")).lower()
            for w in re.findall(r"[a-z]{5,}", txt):
                if w in {"axentx","daemon","docker","python","github",
                         "system","ubuntu","linux","error","failed","cycle"}:
                    continue
                keywords[w] = keywords.get(w, 0) + 1
    except Exception:
        pass
    return sorted(keywords, key=lambda k: -keywords[k])[:30]


# Extra Reddit subs to mine (beyond what reddit-stream daemon covers)
EXTRA_SUBREDDITS = [
    # TH-leaning + Asian dev communities
    "Thailand", "Bangkok", "ProgrammerHumor", "Pantip",
    # Niche dev pain
    "kubernetes", "devops", "sre", "datasets", "datasciencecareers",
    "MachineLearning", "ChatGPTPro", "LocalLLaMA",
    # Compliance/security
    "cybersecurity", "compliance", "ISO27001",
    # Small-business / pre-revenue
    "smallbusiness", "Entrepreneur", "indiehackers", "startups",
    "SaaS", "founders",
]


def discover_github_topics(keywords: list[str], max_per_kw: int = 5) -> list[dict]:
    """For each keyword, GET /search/topics → first N matches NOT yet visited."""
    out = []
    for kw in keywords[:8]:
        url = (f"https://api.github.com/search/topics?"
               f"q={urllib.parse.quote(kw)}&per_page={max_per_kw}")
        body = _get(url, headers={"Authorization": f"Bearer {GH_TOKEN}",
                                  "Accept": "application/vnd.github+json"})
        if not body: continue
        try:
            d = json.loads(body)
        except Exception:
            continue
        for t in d.get("items", [])[:max_per_kw]:
            tname = t.get("name", "")
            if not tname:
                continue
            tu = f"https://github.com/topics/{tname}"
            if _is_visited(tu):
                continue
            _mark_visited(tu, "github-topic", "en")
            out.append({"kind": "github-topic", "name": tname,
                        "url": tu, "description": t.get("short_description", "")[:160],
                        "kw": kw})
    return out


def discover_hn_search(keywords: list[str]) -> list[dict]:
    out = []
    for kw in keywords[:5]:
        url = (f"https://hn.algolia.com/api/v1/search?"
               f"query={urllib.parse.quote(kw)}&tags=story&hitsPerPage=10")
        body = _get(url)
        if not body: continue
        try:
            d = json.loads(body)
        except Exception:
            continue
        for hit in d.get("hits", []):
            sid = hit.get("objectID", "")
            su = f"https://news.ycombinator.com/item?id={sid}"
            if _is_visited(su):
                continue
            _mark_visited(su, "hn-thread", "en")
            out.append({
                "kind": "hn-thread",
                "url": su,
                "title": (hit.get("title") or "")[:160],
                "points": hit.get("points", 0),
                "comments": hit.get("num_comments", 0),
                "kw": kw,
            })
    return out


def discover_reddit_meta(keywords: list[str]) -> list[dict]:
    """Reddit's /r/<sub>/about.json for the EXTRA_SUBREDDITS — get count
    + activity + maybe top weekly posts that include keywords."""
    out = []
    for sub in EXTRA_SUBREDDITS:
        url = f"https://www.reddit.com/r/{sub}/about.json"
        body = _get(url, timeout=10)
        if not body: continue
        try:
            d = json.loads(body)
        except Exception:
            continue
        meta = d.get("data") or {}
        sub_url = f"https://reddit.com/r/{sub}"
        if _is_visited(sub_url):
            continue
        _mark_visited(sub_url, "reddit-sub",
                      "th" if sub in ("Thailand", "Bangkok", "Pantip") else "en")
        out.append({
            "kind": "reddit-sub",
            "url": sub_url,
            "subscribers": meta.get("subscribers", 0),
            "active": meta.get("active_user_count", 0),
            "title": meta.get("public_description", "")[:160],
        })
    return out


def discover_stackexchange(keywords: list[str]) -> list[dict]:
    """SE network search across all sites — multilingual."""
    out = []
    for kw in keywords[:5]:
        url = (f"https://api.stackexchange.com/2.3/search/advanced?"
               f"order=desc&sort=activity&q={urllib.parse.quote(kw)}"
               f"&site=stackoverflow&pagesize=8")
        body = _get(url)
        if not body: continue
        try:
            d = json.loads(body)
        except Exception:
            continue
        for q in d.get("items", []):
            qu = q.get("link", "")
            if not qu or _is_visited(qu):
                continue
            _mark_visited(qu, "stack-q", "en")
            out.append({
                "kind": "stack-q",
                "url": qu,
                "title": (q.get("title") or "")[:160],
                "votes": q.get("score", 0),
                "answers": q.get("answer_count", 0),
                "kw": kw,
            })
    return out


def _research_stage_idle_minutes() -> int:
    """Returns minutes since last research/validator advance. -1 if unknown."""
    if not (SB_URL and SB_KEY):
        return -1
    try:
        cutoff = int(time.time()) - 3600
        qs = urllib.parse.urlencode({
            "or": "(stage.eq.research,stage.eq.validator)",
            "updated_at": f"gte.{cutoff}", "select": "id",
        })
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Prefer": "count=exact", "Range": "0-0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            cr = r.headers.get("Content-Range", "")
            recent = int(cr.split("/")[-1]) if "/" in cr else 0
        return 0 if recent > 0 else 60   # if 0 advances in last hour, "60min idle"
    except Exception:
        return -1


def _last_pull_age_hours() -> float:
    """Hours since last successful source-discover cycle (not yet seen)."""
    try:
        from axentx_shared import kv_get
        v = kv_get("discovery.new_sources") or {}
        if isinstance(v, dict) and v.get("v"): v = v["v"]
        if not isinstance(v, dict) or not v.get("ts"):
            return 99.0
        ts = datetime.datetime.fromisoformat(
            v["ts"].replace("Z", "+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc)
                - ts).total_seconds() / 3600
    except Exception:
        return 99.0


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("source-discover", "  ⤷ not leader — skip")
        return False

    # Trigger logic (event-driven, not cron):
    #   - if research stage idle ≥30min OR last pull was ≥1h ago → fire
    #   - else → idle (no work needed)
    idle_min = _research_stage_idle_minutes()
    last_age_h = _last_pull_age_hours()
    should_fire = idle_min >= 30 or last_age_h >= 1.0
    if not should_fire:
        log("source-discover",
            f"  ⤷ research not stale (idle={idle_min}min, "
            f"last_pull={last_age_h:.1f}h) — skip")
        return False
    log("source-discover",
        f"▸ trigger: research_idle={idle_min}min, "
        f"last_pull={last_age_h:.1f}h ago")

    keywords = fetch_recent_keywords()
    if not keywords:
        # fallback to default broad keywords if no signals yet
        keywords = [
            # core
            "devops", "kubernetes", "ai-agent", "rag", "finops",
            "compliance", "auth", "billing", "saas", "monitoring",
            "observability", "deployment", "scaling",
            # expanded 2026-05-15
            "data-engineering", "ml-platform", "vector-search",
            "edge-computing", "serverless", "platform-engineering", "iac",
            "ci-cd", "secrets-management", "supply-chain-security", "sbom",
            "feature-flags", "incident-management", "chaos-engineering",
            "developer-experience", "api-gateway", "rate-limiting",
            "multi-tenant", "data-lineage", "feature-store",
            # Thai + SEA
            "thai", "soc2", "pdpa", "promptpay", "thai-banking",
            "asean-startup", "shopee-seller", "lazada-seller",
            # vertical SaaS
            "legal-tech", "regtech", "healthtech", "edtech",
            "proptech", "agritech", "logistics-tech", "carbon-tracking",
            "climate-tech", "creator-economy", "remote-work-tools",
        ]