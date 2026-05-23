#!/usr/bin/env python3
"""axentx funding & launch stream — pull pain from FUNDED & LAUNCHING orgs.

User direction (round 2): 'ต่อๆ เอาเยอะๆ'

Funded companies + new launches = verified market validation. Each entry
identifies a domain where investors put real money behind a thesis. The
problems these companies solve = pain you can compete with or extend.

Sources (10):
  1. TechCrunch funding RSS
  2. Crunchbase News (free RSS)
  3. Y Combinator Show HN comments (newest launches)
  4. Indie Hackers milestones (revenue posts)
  5. BetaList latest startups
  6. F6S funding announcements
  7. SaaShub trending products
  8. SaaSworthy newest entries
  9. Reddit /r/Entrepreneur "I made $X" posts
 10. ProductHunt /upcoming (pre-launch validation)

Each item gets 'monetary_signal: high' since they represent
verified-funded plays in a domain.
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
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, new_trace_id  # noqa: E402

CYCLE_GAP_SEC = float(os.environ.get("FUND_CYCLE_GAP_SEC", "300"))
PER_REQ_GAP_SEC = float(os.environ.get("FUND_REQ_GAP_SEC", "5.0"))
MAX_PER_SRC = int(os.environ.get("FUND_MAX_PER_SRC", "15"))
MIN_TITLE_LEN = int(os.environ.get("FUND_MIN_TITLE_LEN", "12"))

UA_POOL = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/120.0.0.0 Safari/537.36"),
]

CF_DEDUP_URL = os.environ.get(
    "CF_DEDUP_URL",
    "https://surrogate-1-cursor.ashira.workers.dev",
).rstrip("/")
_HOST = os.environ.get("HOSTNAME", "funding-launch")
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("funding-launch", "shutdown signal")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _ua():
    return random.choice(UA_POOL)


def _fp(url: str) -> str:
    return hashlib.md5(url.encode("utf-8", errors="replace")).hexdigest()[:16]


def _cf_seen_check(fps):
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


def _cf_seen_mark(fps):
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


def _http_get(url, timeout=12):
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


def _strip_tags(s):
    t = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t)).strip()


def _parse_rss(xml_bytes, source):
    if not xml_bytes:
        return []
    try:
        text = xml_bytes.decode("utf-8", errors="replace")
        text = re.sub(r"xmlns(:\w+)?\s*=\s*\"[^\"]+\"", "", text)
        text = re.sub(r"<(/?)\w+:", r"<\1", text)
        root = ET.fromstring(text)
    except Exception:
        return []
    posts = []
    items = list(root.iter("item")) or list(root.iter("entry"))
    for it in items[:MAX_PER_SRC]:
        t = it.find("title")
        title = (t.text if t is not None else "").strip()
        if len(title) < MIN_TITLE_LEN:
            continue
        l = it.find("link")
        link = ""
        if l is not None:
            link = (l.get("href") or l.text or "").strip()
        if not link:
            continue
        d = (it.find("description") or it.find("summary") or
             it.find("content"))
        body = _strip_tags(d.text or "" if d is not None else "")[:3000]
        posts.append({
            "title": title[:500],
            "body": body[:6000],
            "url": link,
            "score": 0,
            "source": source,
        })
    return posts


# ── source: TechCrunch funding ────────────────────────────────────────
def fetch_techcrunch_funding():
    url = "https://techcrunch.com/category/venture/feed/"
    raw = _http_get(url, timeout=15)
    return _parse_rss(raw, "fund:techcrunch") if raw else []


def fetch_techcrunch_startups():
    url = "https://techcrunch.com/category/startups/feed/"
    raw = _http_get(url, timeout=15)
    return _parse_rss(raw, "fund:tc-startups") if raw else []


# ── source: Crunchbase RSS via news feed ──────────────────────────────
def fetch_crunchbase_news():
    url = "https://news.crunchbase.com/feed/"
    raw = _http_get(url, timeout=12)
    return _parse_rss(raw, "fund:cb-news") if raw else []


# ── source: BetaList ──────────────────────────────────────────────────
def fetch_betalist():
    url = "https://betalist.com/feed"
    raw = _http_get(url, timeout=12)
    return _parse_rss(raw, "fund:betalist") if raw else []


# ── source: Y Combinator Show HN — newest launches with comments ─────
def fetch_yc_show():
    url = ("https://hn.algolia.com/api/v1/search_by_date?query=Show+HN&"
           "tags=story&hitsPerPage=20")
    raw = _http_get(url, timeout=10)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    posts = []
    for h in (data.get("hits") or [])[:MAX_PER_SRC]:
        title = (h.get("title") or "").strip()
        url = h.get("url") or (
            f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        )
        if len(title) < MIN_TITLE_LEN:
            continue
        body = _strip_tags(h.get("story_text") or "") or title
        posts.append({
            "title": f"[Show HN] {title}"[:500],
            "body": body[:6000],
            "url": url,
            "score": int(h.get("points") or 0),
            "source": "fund:yc-show",
        })
    return posts


# ── source: IndieHackers milestones (revenue posts) ──────────────────
def fetch_ih_milestones():
    """IndieHackers /milestones page = founders posting MRR/revenue.
    Each milestone = validated paying customers."""
    url = "https://www.indiehackers.com/milestones"
    raw = _http_get(url, timeout=15)
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    # Find milestone cards: <a href="/milestones/SLUG"> with title nearby
    pat = re.compile(
        r'<a[^>]+href="(/milestones/[^"]+)"[^>]*>([^<]{15,200})</a>',
        re.DOTALL,
    )
    posts = []
    seen = set()
    for m in pat.finditer(text):
        href = m.group(1)
        title = _strip_tags(m.group(2)).strip()
        if href in seen or len(title) < MIN_TITLE_LEN:
            continue
        seen.add(href)
        posts.append({
            "title": f"[IH-milestone] {title}"[:500],
            "body": (f"IndieHackers milestone post — founder reported real "
                     f"revenue/users. Read full post to extract: which niche, "
                     f"what tool stack, what acquisition channel. Each "
                     f"milestone validates a niche has paying customers.")[:6000],
            "url": f"https://www.indiehackers.com{href}",
            "score": 0,
            "source": "fund:ih-milestones",
        })
        if len(posts) >= MAX_PER_SRC:
            break
    return posts


# ── source: ProductHunt /upcoming (pre-launch validation) ────────────
def fetch_ph_upcoming():
    url = "https://www.producthunt.com/upcoming"
    raw = _http_get(url, timeout=15)
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    pat = re.compile(
        r'<a[^>]+href="(/upcoming/[^"/]+)"[^>]*>([^<]{10,150})</a>',
        re.DOTALL,
    )
    posts = []
    seen = set()
    for m in pat.finditer(text):
        href = m.group(1)
        title = _strip_tags(m.group(2)).strip()
        if href in seen or len(title) < MIN_TITLE_LEN:
            continue
        seen.add(href)
        posts.append({
            "title": f"[PH-upcoming] {title}"[:500],
            "body": (f"ProductHunt upcoming product. Founder collecting "
                     f"signups before launch — early validation. The product "
                     f"description reveals the pain they're solving and the "
                     f"audience that signed up.")[:6000],
            "url": f"https://www.producthunt.com{href}",
            "score": 0,
            "source": "fund:ph-upcoming",
        })
        if len(posts) >= MAX_PER_SRC:
            break
    return posts


# ── source: Reddit Entrepreneur "I made $X" posts ─────────────────────
def fetch_entrepreneur_revenue():
    url = ("https://www.reddit.com/r/Entrepreneur/search.json?"
           "q=made+OR+revenue+OR+MRR+OR+ARR&restrict_sr=1&sort=new&t=week&"
           f"limit={MAX_PER_SRC}")
    raw = _http_get(url, timeout=12)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    posts = []
    for c in (data.get("data") or {}).get("children") or []:
        p = c.get("data") or {}
        title = (p.get("title") or "").strip()
        body = (p.get("selftext") or "")[:2000]
        if len(title) < MIN_TITLE_LEN:
            continue
        # Filter: must mention $ or number+k/MRR
        haystack = (title + " " + body).lower()
        if not re.search(r"\$\s*\d|mrr|arr|\d+\s*k\b|\brevenue\b", haystack):
            continue
        url = "https://www.reddit.com" + p.get("permalink", "")
        posts.append({
            "title": f"[r/Entrepreneur-rev] {title}"[:500],
            "body": body[:6000],
            "url": url,
            "score": int(p.get("score") or 0),
            "source": "fund:entrepreneur-revenue",
        })
    return posts


# ── source: SaaSHub trending ──────────────────────────────────────────
def fetch_saashub():
    url = "https://www.saashub.com/popular-products"
    raw = _http_get(url, timeout=15)
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    pat = re.compile(
        r'<a[^>]+href="(/[a-z0-9-]+)"[^>]*class="[^"]*product[^"]*"[^>]*>'
        r'\s*([^<]{5,80})\s*</a>',
        re.DOTALL,
    )
    posts = []
    seen = set()
    for m in pat.finditer(text):
        href = m.group(1)
        title = _strip_tags(m.group(2)).strip()
        if href in seen or len(title) < 5:
            continue
        seen.add(href)
        posts.append({
            "title": f"[SaaSHub] {title}"[:500],
            "body": (f"SaaSHub-popular product '{title}'. Listed on SaaSHub "
                     f"= verified SaaS with paying users. Reviews and "
                     f"alternatives sections expose unmet needs.")[:6000],
            "url": f"https://www.saashub.com{href}",
            "score": 0,
            "source": "fund:saashub",
        })
        if len(posts) >= MAX_PER_SRC:
            break
    return posts


SOURCES = [
    ("techcrunch-vc",    fetch_techcrunch_funding),
    ("tc-startups",      fetch_techcrunch_startups),
    ("crunchbase-news",  fetch_crunchbase_news),
    ("betalist",         fetch_betalist),
    ("yc-show",          fetch_yc_show),
    ("ih-milestones",    fetch_ih_milestones),
    ("ph-upcoming",      fetch_ph_upcoming),
    ("entr-revenue",     fetch_entrepreneur_revenue),
    ("saashub",          fetch_saashub),
]


def make_item(p: dict) -> dict:
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
        "monetary_intent_score": 7,  # funded/launched = high signal
        "history": [{
            "stage": "funding-launch",
            "actor": "funding-launch",
            "output": f"emit (sig=high, src={p['source']})",
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {
            "text": (f"[{p['source']}] {p['title']}\n\n"
                     f"{(p.get('body') or '')[:1500]}"),
        },
    }


def main() -> int:
    log("funding-launch",
        f"streaming {len(SOURCES)} funding/launch sources "
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
                log("funding-launch",
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
                        log("funding-launch",
                            f"  ✓ {p['source']}: {p['title'][:75]}")
                except Exception as e:
                    log("funding-launch",
                        f"  ✗ write: {type(e).__name__}: {str(e)[:60]}")
            if mark_now:
                _cf_seen_mark(mark_now)
            time.sleep(PER_REQ_GAP_SEC)
        elapsed = time.time() - cycle_start
        log("funding-launch",
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
