#!/usr/bin/env python3
"""axentx global job-board stream — 12 international job boards.

User direction 2026-05-10 (round 2): 'ต่อๆ เอาเยอะๆ'

Job postings = companies validated their pain enough to PAY humans.
Internationalizing this gives us regional pain (Asia, EU, LATAM)
that may have less market saturation = better arbitrage for axentx.

Sources (12):
  1. RemoteCo RSS (curated remote)
  2. JustRemote RSS
  3. Working Nomads RSS
  4. Jobspresso RSS
  5. Hacker News Hiring (latest "Who's Hiring" comments via Algolia)
  6. JobInventory RSS
  7. Authentic Jobs RSS
  8. CryptoJobsList API
  9. Web3 Career RSS
 10. AI Jobs RSS (https://aijobs.net)
 11. SimplyHired (via Indeed RSS bridge)
 12. NoFluffJobs (Polish/EU dev jobs)

Each posting = high monetary signal (someone is actively spending
$50K-300K/year to solve a stated problem).
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
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, new_trace_id  # noqa: E402

CYCLE_GAP_SEC = float(os.environ.get("JOB_CYCLE_GAP_SEC", "300"))
PER_REQ_GAP_SEC = float(os.environ.get("JOB_REQ_GAP_SEC", "4.5"))
MAX_PER_SRC = int(os.environ.get("JOB_MAX_PER_SRC", "15"))
MIN_TITLE_LEN = int(os.environ.get("JOB_MIN_TITLE_LEN", "10"))

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

_HOST = os.environ.get("HOSTNAME", "global-jobs")
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("global-jobs", "shutdown signal")


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


def _http_get(url: str, timeout: int = 12) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _ua(),
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,*/*;q=0.8"),
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw
    except Exception:
        return None


def _strip_tags(html_text: str) -> str:
    t = re.sub(r"<script[^>]*>.*?</script>", " ", html_text, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t)).strip()


def _parse_rss(xml_bytes: bytes, source: str) -> list[dict]:
    """Parse generic RSS/Atom feed into post dicts."""
    if not xml_bytes:
        return []
    try:
        # Strip BOM/XML declaration issues
        text = xml_bytes.decode("utf-8", errors="replace")
        # Drop namespaces for easier parsing
        text = re.sub(r"xmlns(:\w+)?\s*=\s*\"[^\"]+\"", "", text)
        text = re.sub(r"<(/?)\w+:", r"<\1", text)
        root = ET.fromstring(text)
    except Exception:
        return []
    posts = []
    # Try RSS items first, then Atom entries
    items = list(root.iter("item")) or list(root.iter("entry"))
    for it in items[:MAX_PER_SRC]:
        title_el = it.find("title")
        title = (title_el.text if title_el is not None else "").strip()
        if len(title) < MIN_TITLE_LEN:
            continue
        link_el = it.find("link")
        link = ""
        if link_el is not None:
            link = (link_el.get("href") or link_el.text or "").strip()
        if not link:
            continue
        desc_el = (it.find("description") or it.find("summary") or
                   it.find("content"))
        desc = desc_el.text if desc_el is not None else ""
        body = _strip_tags(desc or "")[:3000]
        posts.append({
            "title": title[:500],
            "body": body[:6000],
            "url": link,
            "score": 0,
            "source": source,
        })
    return posts


# ── source list ────────────────────────────────────────────────────────
RSS_SOURCES = [
    ("remoteco",        "https://remote.co/remote-jobs/feed/"),
    ("justremote",      "https://justremote.co/remote-developer-jobs.rss"),
    ("workingnomads",   "https://www.workingnomads.com/jobs.rss"),
    ("jobspresso",      "https://jobspresso.co/feed/"),
    ("authenticjobs",   "https://authenticjobs.com/rss/"),
    ("aijobs",          "https://aijobs.net/feed/"),
    ("web3career",      "https://web3.career/jobs.rss"),
    ("nofluffjobs",     "https://nofluffjobs.com/api/posting/feeds/atom"),
]


def fetch_rss_source(name: str, url: str) -> list[dict]:
    raw = _http_get(url, timeout=15)
    if not raw:
        return []
    return _parse_rss(raw, f"jobs:{name}")


def fetch_cryptojobs() -> list[dict]:
    """CryptoJobsList JSON API — public, no auth."""
    url = "https://cryptojobslist.com/api/jobs?limit=20"
    raw = _http_get(url, timeout=10)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    posts = []
    items = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    for j in items[:MAX_PER_SRC]:
        if not isinstance(j, dict):
            continue
        title = (j.get("title") or "").strip()
        company = (j.get("companyName") or j.get("company", "")).strip()
        desc = _strip_tags(j.get("description") or "")[:2000]
        url = j.get("url") or j.get("applyUrl", "")
        if not (title and url):
            continue
        full_title = f"{company} hiring {title}" if company else title
        if len(full_title) < MIN_TITLE_LEN:
            continue
        posts.append({
            "title": full_title[:500],
            "body": desc[:6000],
            "url": url,
            "score": 0,
            "source": "jobs:cryptojobs",
        })
    return posts


def fetch_hn_who_is_hiring() -> list[dict]:
    """Pull recent comments from HN's monthly Who's Hiring thread.
    Filter for paid hire descriptions — each comment is a job listing."""
    posts = []
    url = ("https://hn.algolia.com/api/v1/search?query=Ask+HN+Who+is+hiring"
           "&tags=story&hitsPerPage=2")
    raw = _http_get(url, timeout=10)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    for hit in (data.get("hits") or [])[:1]:
        story_id = hit.get("objectID")
        if not story_id:
            continue
        c_url = (
            f"https://hn.algolia.com/api/v1/search?tags=comment,"
            f"story_{story_id}&hitsPerPage={MAX_PER_SRC * 2}"
        )
        c_raw = _http_get(c_url, timeout=10)
        if not c_raw:
            continue
        try:
            c_data = json.loads(c_raw)
        except Exception:
            continue
        for c in (c_data.get("hits") or [])[:MAX_PER_SRC]:
            txt = _strip_tags(c.get("comment_text") or "")
            if len(txt) < 80:
                continue
            first_line = txt.split("\n")[0][:200]
            posts.append({
                "title": f"[HN-Hiring] {first_line}"[:500],
                "body": txt[:6000],
                "url": (f"https://news.ycombinator.com/item?id="
                        f"{c.get('objectID')}"),
                "score": 0,
                "source": "jobs:hn-whos-hiring",
            })
    return posts


def fetch_simplyhired_remote() -> list[dict]:
    """SimplyHired remote-tech RSS bridge."""
    url = "https://www.simplyhired.com/search-rss?q=remote+software+engineer"
    raw = _http_get(url, timeout=12)
    if not raw:
        return []
    return _parse_rss(raw, "jobs:simplyhired")


def fetch_workable_public() -> list[dict]:
    """Workable hosts public job feeds for many startups via /spi/v3/jobs.
    Aggregator approach: hit the Workable public listing."""
    url = "https://www.workable.com/api/jobs?limit=20"
    raw = _http_get(url, timeout=10)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    posts = []
    items = (data.get("jobs") or data.get("results") or
             (data if isinstance(data, list) else []))
    if not isinstance(items, list):
        return []
    for j in items[:MAX_PER_SRC]:
        if not isinstance(j, dict):
            continue
        title = (j.get("title") or "").strip()
        company = (j.get("company", {}).get("title") or
                   j.get("company", "") or "").strip()
        if isinstance(company, dict):
            company = company.get("title") or ""
        desc = _strip_tags(j.get("description") or
                           j.get("requirements") or "")[:2000]
        url = j.get("url") or j.get("absolute_url") or j.get("apply_url", "")
        if not (title and url):
            continue
        posts.append({
            "title": (f"{company} hiring {title}" if company else title)[:500],
            "body": desc[:6000],
            "url": url,
            "score": 0,
            "source": "jobs:workable",
        })
    return posts


SOURCES = (
    [(name, lambda u=url, n=name: fetch_rss_source(n, u))
     for name, url in RSS_SOURCES]
    + [
        ("cryptojobs",      fetch_cryptojobs),
        ("hn-whos-hiring",  fetch_hn_who_is_hiring),
        ("simplyhired",     fetch_simplyhired_remote),
        ("workable",        fetch_workable_public),
    ]
)


def make_item(p: dict) -> dict:
    """Job postings = HIGH monetary signal."""
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
        "monetary_signal": "high",
        "monetary_intent_score": 7,  # job postings are by definition $$$
        "history": [{
            "stage": "global-jobs",
            "actor": "global-jobs",
            "output": f"emit (sig=high, src={p['source']})",
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {
            "text": (f"[{p['source']}] {p['title']}\n\n"
                     f"{(p.get('body') or '')[:1500]}"),
        },
    }


def main() -> int:
    log("global-jobs",
        f"streaming {len(SOURCES)} global job sources "
        f"(req-gap={PER_REQ_GAP_SEC}s, cycle={CYCLE_GAP_SEC}s)")

    while not _stop:
        cycle_start = time.time()
        emitted = 0
        skipped = 0
        for name, fetcher in SOURCES:
            if _stop:
                break
            try:
                posts = fetcher()
            except Exception as e:
                log("global-jobs",
                    f"  {name} crashed: {type(e).__name__}: "
                    f"{str(e)[:80]}")
                continue
            if not posts:
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
                item = make_item(p)
                try:
                    write_item(item, "validator")
                    mark_now.append(fp)
                    emitted += 1
                    if emitted <= 3:
                        log("global-jobs",
                            f"  ✓ {p['source']}: {p['title'][:75]}")
                except Exception as e:
                    log("global-jobs",
                        f"  ✗ write: {type(e).__name__}: {str(e)[:60]}")
            if mark_now:
                _cf_seen_mark(mark_now)
            time.sleep(PER_REQ_GAP_SEC)
        elapsed = time.time() - cycle_start
        log("global-jobs",
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
