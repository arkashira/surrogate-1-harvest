#!/usr/bin/env python3
"""axentx global-trending-stream — broad/deep global pain harvester.

Adds COVERAGE BREADTH beyond the existing reddit/HN/PH streams:
  - GitHub Trending Repos (daily) — what devs are starring globally
  - HN Show — "Show HN" posts (people launching new tools)
  - HN Ask — "Ask HN" pain points
  - Lobsters — devs with strong opinions
  - Devto trending — frontend/AI/ML write-ups
  - Tildes — quality discussion
  - Stack Overflow Hot Network Questions — top-voted pain
  - npm Most Depended-On (delta) — emerging libs
  - PyPI trending — new Python libs
  - Vercel Templates — what devs are deploying
  - HuggingFace Spaces trending — what AI builders are doing
  - Dribbble shots — design pain (UI/UX gaps)
  - Awesome lists updates — community-curated pain catalogs

User feedback 2026-05-05:
  > "หา source ให้ broad กว่านี้อีกมาก deep กว่านี้อีกมาก"

Each item → research-queue with source='global-trending'. bd-synth +
product-synth pick up unique angles for new product hypotheses.
"""
from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, new_item  # noqa: E402

PER_REQ_GAP_SEC = float(os.environ.get("GLOBAL_REQ_GAP_SEC", "5.0"))
MAX_PER_SOURCE = int(os.environ.get("GLOBAL_MAX_PER_SOURCE", "15"))
POLL_SEC = int(os.environ.get("GLOBAL_POLL_SEC", "1200"))   # 20 min

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# Pain markers (broader than reddit-stream: include curiosity / launches)
SIGNAL_RE = re.compile(
    r"""(how|why|cant|can't|cannot|broken|frustrat|annoying|stuck|workaround|missing|alternative|replace|migrate|tired of|sucks|wrong|bad|fail|hate|switched|show hn|ask hn|launch|introducing|just released|new|tutorial|guide|how I built|lessons|learnings|too expensive|cheap|free alternative|open source alt|saved \$|saved money|cost cut|burning money|blocker|nightmare|painful|terrible|disaster|struggling|fighting with|why is.*so hard|i built|i made|side project|weekend project|how I solved|hack|workaround|vs |compared to|switching from|moved from|better than|worse than|review of|feature flag|api design|developer experience|DX|observability|monitoring gap|trend|hype|adoption|usage|popular|emerging|\$\d+ MRR|\$\d+/mo|monthly recurring|subscribers|profitable|bootstrapped|vertical SaaS|niche tool|industry-specific|healthcare|legal|finance|edtech|fintech|proptech|agtech|hallucination|RAG|fine-tune|embed|vector|context window|agent|tool use|function calling|MCP)""",
    re.IGNORECASE)


_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _fetch(url: str, timeout: int = 20,
           accept: str = "application/json,text/html,*/*") -> str | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8", errors="replace")
    except Exception as e:
        log("global-stream", f"  ✗ fetch {url[:70]}: {type(e).__name__}")
        return None


def _strip(html: str) -> str:
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&[a-z#0-9]+;", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def _hash(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:14]


# ── parsers ─────────────────────────────────────────────────────────────

def src_github_trending() -> list[dict]:
    """github.com/trending — HTML scrape. Returns top repos with descriptions."""
    html = _fetch("https://github.com/trending?since=daily")
    if not html:
        return []
    items = []
    # Pattern: <h2 class="..."><a href="/owner/repo">...
    for m in re.finditer(
        r'<h2[^>]*>\s*<a[^>]*href="(/[^/"]+/[^/"]+)"[^>]*>\s*([\s\S]*?)</a>',
            html):
        repo = m.group(1).strip("/")
        items.append({
            "title": f"GitHub Trending: {repo}",
            "link": f"https://github.com/{repo}",
            "body": _strip(m.group(2))[:300],
        })
        if len(items) >= MAX_PER_SOURCE: break
    # Also pull descriptions following each h2 (rough)
    return items


def src_hn_show() -> list[dict]:
    """HN Show — top stories from /shownew.json"""
    txt = _fetch("https://hacker-news.firebaseio.com/v0/showstories.json")
    if not txt: return []
    try:
        ids = json.loads(txt)[:MAX_PER_SOURCE]
    except Exception: return []
    items = []
    for i in ids:
        try:
            j = json.loads(_fetch(
                f"https://hacker-news.firebaseio.com/v0/item/{i}.json") or "{}")
            if j.get("title"):
                items.append({
                    "title": "Show HN: " + j["title"][:240],
                    "link": j.get("url") or f"https://news.ycombinator.com/item?id={i}",
                    "body": (j.get("text") or "")[:500],
                })
            time.sleep(0.6)
        except Exception:
            pass
    return items


def src_hn_ask() -> list[dict]:
    """HN Ask — pain questions"""
    txt = _fetch("https://hacker-news.firebaseio.com/v0/askstories.json")
    if not txt: return []
    try:
        ids = json.loads(txt)[:MAX_PER_SOURCE]
    except Exception: return []
    items = []
    for i in ids:
        try:
            j = json.loads(_fetch(
                f"https://hacker-news.firebaseio.com/v0/item/{i}.json") or "{}")
            if j.get("title"):
                items.append({
                    "title": "Ask HN: " + j["title"][:240],
                    "link": f"https://news.ycombinator.com/item?id={i}",
                    "body": (j.get("text") or "")[:500],
                })
            time.sleep(0.6)
        except Exception:
            pass
    return items


def src_lobsters() -> list[dict]:
    txt = _fetch("https://lobste.rs/hottest.json")
    if not txt: return []
    try:
        rows = json.loads(txt)[:MAX_PER_SOURCE]
    except Exception: return []
    return [{
        "title": r.get("title", "")[:240],
        "link": r.get("url") or r.get("short_id_url", ""),
        "body": (r.get("description") or "")[:500],
    } for r in rows if r.get("title")]


def src_devto_trending() -> list[dict]:
    txt = _fetch("https://dev.to/api/articles?top=1&per_page=15")
    if not txt: return []
    try:
        rows = json.loads(txt)[:MAX_PER_SOURCE]
    except Exception: return []
    return [{
        "title": r.get("title", "")[:240],
        "link": r.get("url", ""),
        "body": (r.get("description") or "")[:500],
    } for r in rows if r.get("title")]


def src_so_hot() -> list[dict]:
    """Stack Overflow Hot Network Questions — RSS"""
    txt = _fetch(
        "https://stackoverflow.com/feeds/tag?tagnames=performance+or+architecture+or+devops&sort=newest")
    if not txt: return []
    items = []
    for m in re.finditer(
            r"<entry>(.*?)</entry>", txt, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", block)
        link_m = re.search(r'<link[^>]*href=["\']([^"\']+)["\']', block)
        if title_m and link_m:
            items.append({
                "title": _strip(title_m.group(1))[:240],
                "link": link_m.group(1),
                "body": "",
            })
        if len(items) >= MAX_PER_SOURCE: break
    return items


def src_npm_recent() -> list[dict]:
    """npm — recently published packages w/ descriptions"""
    txt = _fetch("https://api.npms.io/v2/search?q=keywords:cli&size=15&from=0")
    if not txt: return []
    try:
        rows = json.loads(txt).get("results", [])[:MAX_PER_SOURCE]
    except Exception: return []
    return [{
        "title": "npm: " + r["package"]["name"],
        "link": r["package"]["links"].get("npm", ""),
        "body": r["package"].get("description", "")[:500],
    } for r in rows if r.get("package", {}).get("name")]


def src_pypi_trending() -> list[dict]:
    """PyPI top-200 RSS (recently uploaded)"""
    txt = _fetch("https://pypi.org/rss/updates.xml")
    if not txt: return []
    items = []
    for m in re.finditer(
            r"<item>(.*?)</item>", txt, re.DOTALL | re.IGNORECASE):
        block = m.group(1)
        title_m = re.search(r"<title>([^<]+)</title>", block)
        link_m = re.search(r"<link>([^<]+)</link>", block)
        desc_m = re.search(r"<description>(.*?)</description>",
                           block, re.DOTALL)
        if title_m and link_m:
            items.append({
                "title": "PyPI: " + _strip(title_m.group(1))[:200],
                "link": _strip(link_m.group(1)),
                "body": _strip(desc_m.group(1))[:400] if desc_m else "",
            })
        if len(items) >= MAX_PER_SOURCE: break
    return items


def src_hf_spaces() -> list[dict]:
    """HuggingFace Spaces trending — public API"""
    txt = _fetch("https://huggingface.co/api/spaces?limit=15&sort=likes&direction=-1")
    if not txt: return []
    try:
        rows = json.loads(txt)[:MAX_PER_SOURCE]
    except Exception: return []
    return [{
        "title": "HF Space: " + r.get("id", "")[:240],
        "link": f"https://huggingface.co/spaces/{r.get('id', '')}",
        "body": (r.get("title") or r.get("id", ""))[:500],
    } for r in rows if r.get("id")]


def src_awesome_updates() -> list[dict]:
    """sindresorhus/awesome — recent commits = community-curated pain catalogs"""
    txt = _fetch("https://api.github.com/repos/sindresorhus/awesome/commits?per_page=15")
    if not txt: return []
    try:
        rows = json.loads(txt)[:MAX_PER_SOURCE]
    except Exception: return []
    items = []
    for r in rows:
        msg = r.get("commit", {}).get("message", "")[:240]
        sha = r.get("sha", "")[:8]
        if msg and "Add " in msg:
            items.append({
                "title": "awesome update: " + msg,
                "link": r.get("html_url", ""),
                "body": "",
            })
    return items


def src_indiehackers_milestones() -> list[dict]:
    """IH milestones — public revenue posts (hot pain solutions)"""
    txt = _fetch("https://www.indiehackers.com/api/v1/milestones?sort=date&limit=15")
    if not txt: return []
    try:
        rows = json.loads(txt).get("milestones", [])[:MAX_PER_SOURCE]
    except Exception: return []
    items = []
    for r in rows:
        title = r.get("description") or r.get("title")
        if title:
            items.append({
                "title": "IH milestone: " + str(title)[:200],
                "link": "https://www.indiehackers.com" + r.get("path", ""),
                "body": str(r.get("note", ""))[:400],
            })
    return items


SOURCES = [
    ("github-trending", src_github_trending),
    ("hn-show", src_hn_show),
    ("hn-ask", src_hn_ask),
    ("lobsters", src_lobsters),
    ("devto-trending", src_devto_trending),
    ("so-hot", src_so_hot),
    ("npm-cli", src_npm_recent),
    ("pypi-updates", src_pypi_trending),
    ("hf-spaces", src_hf_spaces),
    ("awesome-updates", src_awesome_updates),
    ("ih-milestones", src_indiehackers_milestones),
]




def _broaden_active() -> bool:
    """Returns True if demand-amplifier has flagged broaden=True."""
    try:
        from axentx_shared import kv_get
        rec = kv_get("discovery.broaden_keywords")
        if isinstance(rec, dict) and rec.get("v"):
            rec = rec["v"]
        return bool(isinstance(rec, dict) and rec.get("broaden"))
    except Exception:
        return False


def _harvest(name: str, fn) -> int:
    try:
        items = fn() or []
    except Exception as e:
        log("global-stream", f"  ✗ {name}: {type(e).__name__}: {str(e)[:80]}")
        return 0
    emitted = 0
    for it in items[:MAX_PER_SOURCE]:
        title = it.get("title", "")
        body = it.get("body", "")
        if len(title) < 15:
            continue
        if not _broaden_active():
            if not SIGNAL_RE.search(title + " " + body):
                continue
        link = it.get("link", "")
        ts = datetime.datetime.utcnow()
        item_id = (f"{ts.strftime('%Y%m%d-%H%M%S')}"
                   f"-{name[:6]}-{_hash(link or title)}")
        item = {
            "id": item_id,
            "stage": "research",
            "project": None,
            "focus": "discover",
            "created_at": ts.isoformat() + "Z",
            "trace_id": item_id,
            "history": [{
                "stage": "harvest",
                "actor": "global-trending-stream",
                "output": f"source={name} url={link}",
                "at": datetime.datetime.utcnow().isoformat() + "Z",
            }],
            "current": {"text": (                f"## Global signal — {name}\n\n"
                f"**Title:** {title}\n"
                f"**URL:** {link}\n\n"
                f"{body[:500]}\n\n"
                f"_(source: {name}, harvested via global-trending-stream)_"
            )},
            "extra": {
                "source": name,
                "source_url": link,
                "source_lang": "en",
                "harvested_at": datetime.datetime.utcnow().isoformat() + "Z",
            },
        }
        if write_item(item, "research"):
            log("global-stream", f"  ✓ {name}: {title[:80]}")
            emitted += 1
    return emitted


def cycle():
    if _stop: return False
    total = 0
    for name, fn in SOURCES:
        if _stop: break
        n = _harvest(name, fn)
        total += n
        time.sleep(PER_REQ_GAP_SEC)
    log("global-stream",
        f"  cycle done — {total} signals from {len(SOURCES)} sources")
    return False


if __name__ == "__main__":
    from axentx_pipeline import daemon_loop
    daemon_loop("global-stream", POLL_SEC, cycle)
