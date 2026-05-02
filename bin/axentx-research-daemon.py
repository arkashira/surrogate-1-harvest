#!/usr/bin/env python3
"""axentx research daemon — mines real-world pain points.

Sources (no API keys needed for read):
  - Reddit JSON (/r/SaaS, /r/Entrepreneur, /r/codingbusiness, /r/devops, /r/startups, /r/freelance, /r/ChatGPTCoding)
  - Hacker News /show + /ask /top
  - dev.to API
  - Indie Hackers public posts (RSS)

For every interesting post (sort=hot, top last 24h), pull title + selftext
and ask the LLM to: extract the underlying pain point in 1-2 sentences,
classify domain, score severity 0-10. Output a research-report record
into the `research-queue` for the BD daemon to triage.

Multiple instances of this daemon can run side-by-side (RESEARCH_WORKER_ID
env distinguishes them); each picks a different subreddit/source on each
cycle so coverage scales linearly with worker count.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, write_item, daemon_loop,
                             rag_top_score, new_trace_id, get_role_budget)

# Vectorize-based dedup: if the new pain matches a past pain at this cosine
# score or higher, skip it. 0.85 = "essentially the same problem rephrased".
DEDUP_SIM_THRESHOLD = float(os.environ.get("RESEARCH_DEDUP_THRESHOLD", "0.85"))
RESEARCH_BUDGET = get_role_budget("research", 400)

WORKER_ID = os.environ.get("RESEARCH_WORKER_ID", "1")
# 30s = effectively continuous (was 600s = "cron-y"). Each cycle still hits
# SOURCES_PER_CYCLE distinct subreddits/HN/devto/IH. With 3 workers x 30s
# per cycle x 3 sources/cycle, fleet covers ~1080 source-hits/h vs the
# old 18/h. Coverage of all 14 subreddits cycles every ~5 min instead of
# every 8h. (User directive 2026-05-02 round 3: 'ไม่ควรมีการทำเป็น cron'.)
POLL_SEC = int(os.environ.get("RESEARCH_POLL_SEC", "30"))
SOURCES_PER_CYCLE = int(os.environ.get("RESEARCH_SOURCES_PER_CYCLE", "3"))

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Subreddits surface real pain — we filter for "actually a problem" via the LLM.
SUBREDDITS = [
    "SaaS", "Entrepreneur", "codingbusiness", "devops", "startups",
    "freelance", "ChatGPTCoding", "selfhosted", "kubernetes", "cybersecurity",
    "MachineLearning", "OpenAI", "ProductManagement", "ExperiencedDevs",
]

CURSOR_FILE = REPO_ROOT / "state" / f"axentx-research-cursor-{WORKER_ID}.json"


RESEARCH_SYSTEM = """You are a market researcher mining real-world pain points
from internet posts. For each post, output strict JSON:

{
  "is_real_pain": true|false,
  "pain_one_liner": "the specific problem in 1 sentence",
  "domain": "saas|devops|security|ml|productivity|finance|education|other",
  "severity": 1-10,
  "audience": "who suffers from this (1 phrase)",
  "evidence": "quote a sentence from the post that proves it's real",
  "search_for_dupes": "3 short search queries to validate this is a recurring pain"
}

REJECT (set is_real_pain=false) if the post is:
- self-promotion / launch / "look what I built"
- venting without a specific problem
- already-solved questions where good answers exist in comments
- pure opinion / discussion / news commentary

ACCEPT only if the post describes a CONCRETE problem the author wants solved
and that affects more than just them. Severity scales with: how often does
this hurt, how much money/time it wastes, how many people share it.\nGROUNDING: Cite at least one concrete source for every claim (URL from the post, dataset/repo name, established framework name, published number). If you cannot cite, say "unverified — needs research" instead of fabricating a number, market size, competitor name, or feature claim. Made-up references are worse than honest gaps.
"""


def fetch_reddit(sub: str) -> list[dict]:
    """Reddit blocks datacenter UA on www; old.reddit + RSS often slips through.
    Try in order: old.reddit JSON → RSS feed. Fallback empty on full block.

    Browser UA (not 'Surrogate1Bot/...') because Reddit's anti-scrape
    aggressively 403s any non-browser ID even with proper reddit:bot
    spec. We're a polite reader (low rate, no auth) — not abuse."""
    headers = {
        "User-Agent": UA,  # browser UA defined at module top
        "Accept": "application/json",
    }
    posts = []
    for url in (f"https://old.reddit.com/r/{sub}/hot.json?limit=15&t=day",
                f"https://www.reddit.com/r/{sub}/hot.json?limit=15&t=day"):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
            for c in (d.get("data") or {}).get("children", [])[:15]:
                p = c.get("data") or {}
                if p.get("stickied") or (p.get("score", 0) < 5):
                    continue
                body = (p.get("selftext") or "").strip()
                if len(body) < 80: continue
                posts.append({
                    "source": f"reddit/r/{sub}",
                    "url": "https://reddit.com" + p.get("permalink", ""),
                    "title": p.get("title", "")[:300],
                    "body": body[:3000],
                    "score": p.get("score", 0),
                    "num_comments": p.get("num_comments", 0),
                })
            return posts
        except urllib.error.HTTPError as e:
            if e.code in (403, 429): continue  # try next URL
            return posts
        except Exception:
            return posts
    return posts


def fetch_lobsters() -> list[dict]:
    """Lobsters — engineering-focused HN alternative. JSON API, no auth."""
    req = urllib.request.Request(
        "https://lobste.rs/hottest.json",
        headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        items = json.loads(r.read())
    posts = []
    for p in items[:20]:
        body = (p.get("description_plain") or p.get("description") or "").strip()
        if len(body) < 80: continue
        posts.append({
            "source": "lobsters",
            "url": p.get("url") or p.get("short_id_url",""),
            "title": p.get("title","")[:300],
            "body": body[:3000],
            "score": p.get("score", 0),
            "num_comments": len(p.get("comments") or []),
        })
    return posts


def fetch_indiehackers() -> list[dict]:
    """Indie Hackers via RSS — entrepreneurs talk about real pains."""
    import xml.etree.ElementTree as ET
    req = urllib.request.Request(
        "https://www.indiehackers.com/feed.xml",
        headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            xml = r.read()
        root = ET.fromstring(xml)
    except Exception:
        return []
    posts = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in (root.findall(".//item") or root.findall(".//atom:entry", ns))[:15]:
        title = (entry.findtext("title") or
                 entry.findtext("atom:title", default="", namespaces=ns) or "")
        link = (entry.findtext("link") or
                (entry.find("atom:link", ns).get("href") if entry.find("atom:link", ns) is not None else ""))
        body = (entry.findtext("description") or
                entry.findtext("atom:summary", default="", namespaces=ns) or "")
        if len(body) < 80: continue
        posts.append({
            "source": "indiehackers",
            "url": link, "title": title[:300],
            "body": body[:3000], "score": 0, "num_comments": 0,
        })
    return posts


def fetch_hn() -> list[dict]:
    """Hacker News — top stories of last day."""
    ids_url = "https://hacker-news.firebaseio.com/v0/topstories.json"
    req = urllib.request.Request(ids_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        ids = json.loads(r.read())[:30]
    posts = []
    for sid in ids[:15]:
        try:
            req = urllib.request.Request(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                headers={"User-Agent": UA},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                p = json.loads(r.read())
            text = (p.get("text") or "").strip() or p.get("title", "")
            if not text or len(text) < 50:
                continue
            posts.append({
                "source": "hn",
                "url": f"https://news.ycombinator.com/item?id={sid}",
                "title": p.get("title", "")[:300],
                "body": text[:3000],
                "score": p.get("score", 0),
                "num_comments": p.get("descendants", 0),
            })
        except Exception:
            continue
    return posts


def fetch_devto() -> list[dict]:
    url = "https://dev.to/api/articles/latest?per_page=15"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read())
    posts = []
    for p in data[:15]:
        body = (p.get("description") or "").strip()
        if len(body) < 80:
            continue
        posts.append({
            "source": "devto",
            "url": p.get("url", ""),
            "title": p.get("title", "")[:300],
            "body": body[:3000],
            "score": p.get("public_reactions_count", 0),
            "num_comments": p.get("comments_count", 0),
        })
    return posts


# ─── Reddit OAuth (script-app) — bypasses anti-scrape on shared GCP NAT ──
# Why: Reddit blocks datacenter IPs for unauthenticated JSON. The official
# script-app OAuth flow is free, gives us 600 reqs/10min/app, and works from
# anywhere. Per user directive (2026-05-02): "หา proxy agent หรือ solution
# bypass" — OAuth IS the solution; no proxy needed when you're authenticated.
# Set REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET in /etc/surrogate-coordinator.env
# (created at https://www.reddit.com/prefs/apps → "create app" → "script").
# REDDIT_USERNAME optional but recommended for the User-Agent header.
_reddit_token: dict[str, float | str] = {"token": "", "expires_at": 0.0}


def _reddit_oauth_token() -> str:
    """Return a cached bearer token; refresh ~10s before expiry."""
    cid = os.environ.get("REDDIT_CLIENT_ID", "")
    csec = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not (cid and csec):
        return ""
    if _reddit_token["token"] and time.time() < float(_reddit_token["expires_at"]):
        return str(_reddit_token["token"])
    import base64
    auth = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    body = b"grant_type=client_credentials"
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=body, method="POST",
        headers={
            "Authorization": f"Basic {auth}",
            "User-Agent": f"axentx-research/0.2 (by /u/{os.environ.get('REDDIT_USERNAME','axentx')})",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        _reddit_token["token"] = d.get("access_token", "")
        _reddit_token["expires_at"] = time.time() + int(d.get("expires_in", 3600)) - 30
        return str(_reddit_token["token"])
    except Exception:
        return ""


def fetch_reddit_oauth(sub: str) -> list[dict]:
    """OAuth-authenticated Reddit fetch. Returns [] if no creds → caller
    falls through to legacy old.reddit JSON path."""
    tok = _reddit_oauth_token()
    if not tok:
        return []
    posts: list[dict] = []
    url = f"https://oauth.reddit.com/r/{sub}/hot?limit=15&t=day"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tok}",
        "User-Agent": f"axentx-research/0.2 (by /u/{os.environ.get('REDDIT_USERNAME','axentx')})",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
    except Exception:
        return []
    for c in (d.get("data") or {}).get("children", [])[:15]:
        p = c.get("data") or {}
        if p.get("stickied") or (p.get("score", 0) < 5):
            continue
        body = (p.get("selftext") or "").strip()
        if len(body) < 80:
            continue
        posts.append({
            "source": f"reddit/r/{sub}",
            "url": "https://reddit.com" + p.get("permalink", ""),
            "title": p.get("title", "")[:300],
            "body": body[:3000],
            "score": p.get("score", 0),
            "num_comments": p.get("num_comments", 0),
        })
    return posts


def fetch_reddit_smart(sub: str) -> list[dict]:
    """OAuth first; fall back to anonymous old.reddit on creds-missing."""
    posts = fetch_reddit_oauth(sub)
    if posts:
        return posts
    return fetch_reddit(sub)


# ─── StackExchange (Stack Overflow + adjacent sites) ──────────────────────
# Free API, 300 reqs/day no auth — perfect for daily harvest of "things devs
# are stuck on". `tagged` filter gives us topical bands (devops/aws/etc.).
def fetch_stackexchange(site_tag: str) -> list[dict]:
    """site_tag = '<site>:<tag>' e.g. 'stackoverflow:devops' / 'serverfault:aws'."""
    site, tag = site_tag.split(":", 1)
    url = (f"https://api.stackexchange.com/2.3/questions"
           f"?order=desc&sort=creation&site={site}&tagged={tag}"
           f"&pagesize=15&filter=!9_bDDxJY5")  # filter includes body
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            import gzip
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            d = json.loads(raw)
    except Exception:
        return []
    posts: list[dict] = []
    for q in (d.get("items") or [])[:15]:
        body_html = q.get("body") or ""
        # strip HTML tags crudely (keeping text content)
        body = re.sub(r"<[^>]+>", " ", body_html)
        body = re.sub(r"\s+", " ", body).strip()
        if len(body) < 80:
            continue
        posts.append({
            "source": f"stackexchange/{site}/{tag}",
            "url": q.get("link", ""),
            "title": q.get("title", "")[:300],
            "body": body[:3000],
            "score": q.get("score", 0),
            "num_comments": q.get("answer_count", 0),
        })
    return posts


# ─── ProductHunt — RSS feed of daily launches ─────────────────────────────
def fetch_producthunt() -> list[dict]:
    """RSS doesn't include vote count, but tag/text alone is enough signal
    to spot recurring pain themes (categories that dominate launches today
    = where market is hottest)."""
    import xml.etree.ElementTree as ET
    try:
        req = urllib.request.Request(
            "https://www.producthunt.com/feed", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            xml = r.read()
        root = ET.fromstring(xml)
    except Exception:
        return []
    posts: list[dict] = []
    for item in root.findall(".//item")[:15]:
        title = (item.findtext("title") or "")[:300]
        link = item.findtext("link") or ""
        body = (item.findtext("description") or "").strip()
        if len(body) < 80:
            continue
        # Strip HTML
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        posts.append({
            "source": "producthunt",
            "url": link, "title": title,
            "body": body[:3000], "score": 0, "num_comments": 0,
        })
    return posts


# ─── GitHub Issues — search popular bug reports / feature requests ────────
def fetch_github_issues(query: str) -> list[dict]:
    """Issues with > N reactions surface real recurring pain across thousands
    of repos. Free 30/min unauthenticated (60/min if GH_TOKEN set)."""
    tok = os.environ.get("AXENTX_BOT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    url = (f"https://api.github.com/search/issues"
           f"?q={urllib.parse.quote(query)}"
           f"&sort=reactions&order=desc&per_page=10")
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
    except Exception:
        return []
    posts: list[dict] = []
    for it in (d.get("items") or [])[:10]:
        body = (it.get("body") or "").strip()
        if len(body) < 80:
            continue
        title = (it.get("title") or "")[:300]
        posts.append({
            "source": "github-issues",
            "url": it.get("html_url", ""),
            "title": title,
            "body": body[:3000],
            "score": (it.get("reactions") or {}).get("total_count", 0),
            "num_comments": it.get("comments", 0),
        })
    return posts


# ─── archive.org Wayback fallback for any URL ─────────────────────────────
# When a primary fetch returns 404/410/403/blocked, we fall through to the
# Wayback closest-snapshot API. Useful when the original page is taken down
# or geo-blocked but still indexed. Free, no auth.
def archive_snapshot(target_url: str) -> str | None:
    """Return raw text of the latest archived snapshot, or None."""
    api = f"http://archive.org/wayback/available?url={urllib.parse.quote(target_url)}"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read())
        snap = (d.get("archived_snapshots") or {}).get("closest") or {}
        if not snap.get("available") or not snap.get("url"):
            return None
        snap_url = snap["url"]
        if snap_url.startswith("http://"):
            snap_url = "https://" + snap_url[len("http://"):]
        req2 = urllib.request.Request(snap_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req2, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")[:8000]
    except Exception:
        return None


# ─── CF Worker /probe fan-out — IP rotation for blocked targets ──────────
# Hits surrogate-1-cursor.ashira.workers.dev/probe?url=... which fetches
# from CF egress (different IP pool than GCP NAT). Useful when GCP gets
# 429/403 but CF doesn't.
PROBE_URL = os.environ.get(
    "PROBE_URL",
    "https://surrogate-1-cursor.ashira.workers.dev/probe",
)
PROBE_AUTH = os.environ.get("PROBE_AUTH", "")


def probe_via_cf(target_url: str) -> str | None:
    """Fetch via CF Worker. Returns body text or None on failure."""
    qs = urllib.parse.urlencode({"url": target_url})
    headers = {"User-Agent": UA}
    if PROBE_AUTH:
        headers["X-Probe-Token"] = PROBE_AUTH
    try:
        req = urllib.request.Request(f"{PROBE_URL}?{qs}", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            status = r.headers.get("X-Probe-Status", "0")
            if not (status.startswith("2") or status.startswith("3")):
                return None
            return r.read().decode("utf-8", errors="replace")[:8000]
    except Exception:
        return None


# StackExchange topical bands — each gives ~15 questions per cycle.
SE_TARGETS = [
    "stackoverflow:devops", "stackoverflow:aws", "stackoverflow:terraform",
    "stackoverflow:kubernetes", "stackoverflow:llm", "stackoverflow:rag",
    "serverfault:aws", "softwareengineering:architecture",
]
# GitHub issues queries — frame as "explicit pain"
GH_QUERIES = [
    "is:issue is:open label:bug reactions:>100 created:>2026-04-01",
    'is:issue is:open in:body "this is impossible" reactions:>5',
    'is:issue is:open in:body "I wish" label:enhancement reactions:>20',
    "is:issue is:open in:title \"can't\" reactions:>50",
]

# Non-Reddit sources FIRST so workers produce signal even when Reddit's
# anti-scrape blocks our IPs (OAuth fixes most of this but kept resilient).
SOURCES = (
    [("hn", fetch_hn), ("devto", fetch_devto),
     ("lobsters", fetch_lobsters), ("indiehackers", fetch_indiehackers),
     ("producthunt", fetch_producthunt)]
    + [(f"se:{tag}", lambda t=tag: fetch_stackexchange(t)) for tag in SE_TARGETS]
    + [(f"gh:{q[:30]}", lambda q=q: fetch_github_issues(q)) for q in GH_QUERIES]
    + [(f"reddit:{s}", lambda s=s: fetch_reddit_smart(s)) for s in SUBREDDITS]
)


SEEN_TTL_SEC = int(os.environ.get("RESEARCH_SEEN_TTL_SEC", "604800"))   # 7 days
SEEN_CAP = int(os.environ.get("RESEARCH_SEEN_CAP", "1500"))             # was 5000

# Cross-VM shared dedup. Migrated 2026-05-03 from CF Worker /seen/* to
# Supabase RPC seen_check_bulk + seen_mark_bulk. CF was rate-limited at
# 100k req/day with 572 daemons; Supabase free tier has no per-request cap.
SB_URL = os.environ.get("SUPABASE_URL", "https://riunimyxoalicbntogbp.supabase.co")
SB_KEY = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get(
    "SUPABASE_SERVICE_KEY", ""
)
SHARED_DEDUP_URL = os.environ.get("SHARED_DEDUP_URL", "")  # legacy CF fallback
HF_STAGING_URL = os.environ.get("HF_STAGING_URL", "")
import socket as _socket
_HOST = _socket.gethostname()


def shared_dedup_check(fps: list[str]) -> set[str]:
    """Return the SUBSET of fps that the shared store has NOT seen yet."""
    if not fps:
        return set()
    fps_chunk = fps[:200]
    # Supabase first
    if SB_URL and SB_KEY:
        try:
            req = urllib.request.Request(
                f"{SB_URL}/rest/v1/rpc/seen_check_bulk",
                data=json.dumps({"p_kind": "pain-url", "p_fps": fps_chunk}).encode(),
                method="POST",
                headers={
                    "apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
                    "Content-Type": "application/json", "User-Agent": UA,
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read())
            seen = {row.get("fp") for row in d if isinstance(row, dict)}
            return set(fps_chunk) - seen
        except Exception:
            pass
    # CF fallback
    if SHARED_DEDUP_URL:
        body = json.dumps({"kind": "pain-url", "fps": fps_chunk}).encode()
        req = urllib.request.Request(
            f"{SHARED_DEDUP_URL}/seen/check", data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read())
            return set(d.get("unseen") or [])
        except Exception:
            pass
    # Fail-open: if everything is unreachable, treat all as unseen so research
    # doesn't block. Local cursor still dedups within this worker's session.
    return set(fps_chunk)


def shared_dedup_mark(fps: list[str]) -> None:
    if not fps:
        return
    fps_chunk = fps[:200]
    if SB_URL and SB_KEY:
        try:
            req = urllib.request.Request(
                f"{SB_URL}/rest/v1/rpc/seen_mark_bulk",
                data=json.dumps({"p_kind": "pain-url", "p_fps": fps_chunk,
                                 "p_host": _HOST}).encode(),
                method="POST",
                headers={
                    "apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
                    "Content-Type": "application/json", "User-Agent": UA,
                },
            )
            urllib.request.urlopen(req, timeout=10)
            return
        except Exception:
            pass
    if SHARED_DEDUP_URL:
        body = json.dumps({"kind": "pain-url", "fps": fps_chunk, "host": _HOST}).encode()
        req = urllib.request.Request(
            f"{SHARED_DEDUP_URL}/seen/mark", data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass  # best-effort


def stage_post_to_hf_buffer(post: dict) -> None:
    """Stage raw post to harvested_pains table (Supabase). hf-flusher-daemon
    drains pushed_to_hf=0 rows to axentx/surrogate-1-harvested-pains every
    cycle. Migrated from D1 (CF) → Supabase 2026-05-03."""
    payload = {
        "source": post.get("source", ""),
        "url": post.get("url", ""),
        "title": post.get("title", "")[:500],
        "body": post.get("body", "")[:6000],
        "score": int(post.get("score", 0) or 0),
    }
    if SB_URL and SB_KEY:
        try:
            req = urllib.request.Request(
                f"{SB_URL}/rest/v1/harvested_pains",
                data=json.dumps(payload).encode(), method="POST",
                headers={
                    "apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
                    "Content-Type": "application/json", "User-Agent": UA,
                    "Prefer": "return=minimal",
                },
            )
            urllib.request.urlopen(req, timeout=8)
            return
        except Exception:
            pass
    # CF fallback
    if HF_STAGING_URL:
        try:
            req = urllib.request.Request(
                HF_STAGING_URL, data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json", "User-Agent": UA},
            )
            urllib.request.urlopen(req, timeout=8)
        except Exception:
            pass


def load_cursor() -> dict:
    if CURSOR_FILE.exists():
        try:
            c = json.loads(CURSOR_FILE.read_text())
            # Migration: legacy format had seen=list[fingerprint]. New
            # format = list[{fp, ts}]. Auto-upgrade legacy entries with
            # ts=now (so they all expire together at +TTL — preferable to
            # losing them outright).
            seen = c.get("seen", [])
            if seen and isinstance(seen[0], str):
                now_ts = int(time.time())
                c["seen"] = [{"fp": fp, "ts": now_ts} for fp in seen]
            return c
        except Exception:
            pass
    return {"src_idx": (int(WORKER_ID) - 1) % len(SOURCES), "seen": []}


def save_cursor(c: dict) -> None:
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    # TTL prune: drop fingerprints older than SEEN_TTL_SEC. This lets posts
    # we saw N days ago resurface — useful when pain pattern recurs (often
    # the same post gets re-asked across many users) and PREVENTS the
    # cursor from saturating to the point where every fetch is fully
    # deduped (observed 2026-05-02: every research cycle returned
    # new_seen=0 because seen-list had absorbed thousands of posts).
    cutoff = int(time.time()) - SEEN_TTL_SEC
    pruned = [s for s in c.get("seen", [])
              if isinstance(s, dict) and s.get("ts", 0) >= cutoff]
    c["seen"] = pruned[-SEEN_CAP:]
    CURSOR_FILE.write_text(json.dumps(c, indent=2))


def post_fingerprint(post: dict) -> str:
    return hashlib.sha256(
        (post.get("source", "") + "|" + post.get("url", "")).encode()
    ).hexdigest()[:16]


def do_one_cycle() -> bool:
    c = load_cursor()
    # Local seen (cursor file) — fast, but per-VM. Shared dedup (CF D1)
    # handles cross-VM coordination.
    seen = set(s["fp"] for s in c.get("seen", []) if isinstance(s, dict))
    fired = 0
    new_fps_to_mark: list[str] = []  # batch shared-mark at cycle end

    for _ in range(SOURCES_PER_CYCLE):
        idx = c["src_idx"] % len(SOURCES)
        name, fetcher = SOURCES[idx]
        c["src_idx"] += 1
        log(f"research-{WORKER_ID}", f"▸ pulling {name}")
        try:
            posts = fetcher()
        except Exception as e:
            log(f"research-{WORKER_ID}", f"  ✗ {name}: {type(e).__name__}: {str(e)[:80]}")
            time.sleep(2)
            continue
        n_total = len(posts)
        n_dup = 0; n_seen = 0; n_rejected = 0; n_low_sev = 0; n_dedupe = 0

        # Stage raw posts to HF staging buffer (best-effort, fire-and-forget)
        for p in posts[:15]:
            stage_post_to_hf_buffer(p)

        # Cross-VM dedup: ask shared store which fps are still unseen.
        # Local cursor is the fast path; shared store catches the cross-VM case.
        all_fps = [post_fingerprint(p) for p in posts]
        local_unseen = [fp for fp in all_fps if fp not in seen]
        if local_unseen:
            shared_unseen = shared_dedup_check(local_unseen)
        else:
            shared_unseen = set()

        for post in posts:
            fp = post_fingerprint(post)
            if fp in seen:
                n_seen += 1
                continue
            if fp not in shared_unseen and local_unseen:
                # Another VM already saw this URL — skip to save LLM tokens
                seen.add(fp)
                n_seen += 1
                continue
            seen.add(fp)
            new_fps_to_mark.append(fp)

            # Ask LLM if this is a real pain point worth chasing
            prompt = (
                f"Source: {post['source']}\n"
                f"Title: {post['title']}\n"
                f"Score: {post['score']} | Comments: {post['num_comments']}\n"
                f"URL: {post['url']}\n\n"
                f"Body:\n{post['body']}\n\n"
                f"Output strict JSON only — no commentary."
            )
            try:
                out = call_llm(prompt, system=RESEARCH_SYSTEM,
                               max_tokens=RESEARCH_BUDGET, timeout=30)
                # Extract JSON (LLM sometimes wraps in code fences)
                txt = out.strip()
                if "```" in txt:
                    txt = txt.split("```")[1]
                    if txt.startswith("json"):
                        txt = txt[4:]
                verdict = json.loads(txt.strip())
            except Exception as e:
                log(f"research-{WORKER_ID}", f"  ⚠ LLM/parse fail on {post['url'][:60]}: {str(e)[:60]}")
                continue

            if not verdict.get("is_real_pain"):
                n_rejected += 1
                continue
            if verdict.get("severity", 0) < 4:
                n_low_sev += 1
                continue  # noise filter (was 5, lowered 2026-05-02)

            # Dedup against the RAG corpus — if a near-identical pain has
            # been mined before, skip rather than re-running the whole
            # downstream pipeline on a duplicate idea.
            pain_text = verdict.get("pain_one_liner") or ""
            if pain_text:
                sim = rag_top_score(pain_text, kind="pain")
                if sim >= DEDUP_SIM_THRESHOLD:
                    n_dedupe += 1
                    log(f"research-{WORKER_ID}",
                        f"  ⤳ dedup (sim {sim:.2f}≥{DEDUP_SIM_THRESHOLD}): "
                        f"{pain_text[:60]}")
                    continue

            # Push to research-queue for BD daemon. discovery_id stays with
            # the item through every advance — write-once, never overwritten.
            ts_iso = datetime.datetime.utcnow().isoformat() + "Z"
            ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            item_id = f"{ts}-pain-{fp}"
            discovery_id = new_trace_id()
            item = {
                "id": item_id,
                "discovery_id": discovery_id,
                "trace_id": discovery_id,  # one trace per opportunity end-to-end
                "stage": "research",
                "created_at": ts_iso,
                "post": post,
                "verdict": verdict,
                "history": [{
                    "stage": "research",
                    "actor": f"axentx-research-{WORKER_ID}",
                    "output": json.dumps(verdict, ensure_ascii=False),
                    "at": ts_iso,
                }],
                "current": {"text": json.dumps(verdict, ensure_ascii=False)},
            }
            # next stage = pain-validator (cross-source confirmation gate);
            # validator promotes to bd-queue or rejects to done/
            write_item(item, "validator")
            log(f"research-{WORKER_ID}",
                f"  ✓ pain (sev {verdict.get('severity')}, "
                f"disc={discovery_id[:8]}): "
                f"{verdict.get('pain_one_liner','')[:70]}")
            fired += 1

        log(f"research-{WORKER_ID}",
            f"  source {name}: total={n_total} new_seen={n_total-n_seen} "
            f"rejected={n_rejected} low_sev={n_low_sev} dedup={n_dedupe} "
            f"→ fired_so_far={fired} (→ validator-queue)")

    # Merge new fingerprints with existing TS-tagged entries. Old entries
    # keep their original timestamp; new fingerprints (those NOT already
    # in old seen) get the current timestamp so TTL kicks in correctly.
    now_ts = int(time.time())
    old_by_fp = {s["fp"]: s for s in c.get("seen", []) if isinstance(s, dict)}
    merged = []
    for fp in seen:
        if fp in old_by_fp:
            merged.append(old_by_fp[fp])
        else:
            merged.append({"fp": fp, "ts": now_ts})
    c["seen"] = merged
    save_cursor(c)
    # Mark fingerprints as seen in the shared cross-VM store
    if new_fps_to_mark:
        shared_dedup_mark(new_fps_to_mark)
    log(f"research-{WORKER_ID}", f"cycle done — {fired} new pain items pushed → bd-queue (cross-VM marked: {len(new_fps_to_mark)})")
    return fired > 0


if __name__ == "__main__":
    daemon_loop(f"research-{WORKER_ID}", POLL_SEC, do_one_cycle)
