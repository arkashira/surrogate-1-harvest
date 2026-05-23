#!/usr/bin/env python3
"""axentx Global Trend Harvester — TRACK C: foreign-market trends → Thai arbitrage.

User direction 2026-05-10:
  > 'หา business trend ทั่วโลก... โลกเค้าทำอะไรกันอยู่ ในไทยมีหรือยัง
     ถ้ายัง TAM SAM SOM เป็นยังไง น่าสนใจไหม... ทุกอุตสาหกรรม ไม่ใช่แค่ IT
     อาหารก็ได้ SaaS ก็ได้ ทำเงินได้พอ'

This daemon harvests trend signals from 70+ sources across:
  • Cross-vertical trend aggregators (TrendHunter, Springwise, PSFK)
  • Industry-specific (food, fashion, beauty, fintech, healthtech, EV, gaming, crypto)
  • Regional / foreign-market (China, Japan, Korea, India, LATAM, EU, Africa, MENA)
  • Consumer demand signals (Google Trends, Amazon movers, Etsy, Reddit Trends)
  • Investment trends (TC, YC, a16z, Sequoia, General Catalyst)

Output: items written to `trend-raw` queue with metadata:
  - source: trendhunter / springwise / 36kr / etc.
  - region: global / asia / sea / cn / jp / kr / in / latam / eu / af
  - category_hint: food / saas / beauty / fashion / health / fintech / etc.

Downstream: `axentx-trend-arbitrage-daemon` extracts the trend, checks
Thai market presence, computes TAM/SAM/SOM, scores arbitrage potential,
routes high-scoring trends to biz-research-queue (TRACK B).
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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, new_trace_id  # noqa: E402

CYCLE_GAP_SEC = float(os.environ.get("TREND_CYCLE_GAP_SEC", "600"))
PER_REQ_GAP_SEC = float(os.environ.get("TREND_REQ_GAP_SEC", "5.0"))
MAX_PER_SRC = int(os.environ.get("TREND_MAX_PER_SRC", "12"))
MIN_TITLE_LEN = int(os.environ.get("TREND_MIN_TITLE_LEN", "12"))

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
_HOST = os.environ.get("HOSTNAME", "global-trends")
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("global-trends", "shutdown signal")


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
            data=json.dumps({"kind": "trend-url", "fps": fps[:200]}).encode(),
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
                "kind": "trend-url", "fps": fps[:200], "host": _HOST,
            }).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": _ua()},
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def _http_get(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _ua(),
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,application/json;q=0.95,*/*;q=0.8"),
            "Accept-Language": ("en-US,en;q=0.7,th;q=0.5,ja;q=0.4,"
                                "zh;q=0.3,ko;q=0.3"),
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
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t)).strip()


def _parse_rss(xml_bytes, source, region="global", cat="general"):
    """Return posts annotated with region + category hint."""
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
        cat_el = it.find("category")
        cat_hint = (cat_el.text if cat_el is not None else cat).strip() or cat
        posts.append({
            "title": title[:500],
            "body": body[:6000],
            "url": link,
            "score": 0,
            "source": source,
            "region": region,
            "category_hint": cat_hint[:40].lower(),
        })
    return posts


# ── SOURCE CATALOG: 70+ across 6 axes ────────────────────────────────
# (source_name, url, region, category_hint)
RSS_SOURCES = [
    # ── Cross-vertical trend aggregators (10) ──
    ("trendhunter",        "https://www.trendhunter.com/rss/category/Business",  "global",  "general"),
    ("trendhunter-tech",   "https://www.trendhunter.com/rss/category/Technology", "global",  "tech"),
    ("trendhunter-fashion", "https://www.trendhunter.com/rss/category/Fashion",  "global",  "fashion"),
    ("trendhunter-food",   "https://www.trendhunter.com/rss/category/Food",      "global",  "food"),
    ("trendhunter-life",   "https://www.trendhunter.com/rss/category/Lifestyle", "global",  "lifestyle"),
    ("springwise",         "https://www.springwise.com/feed/",                   "global",  "innovation"),
    ("psfk",               "https://www.psfk.com/feed",                          "global",  "innovation"),
    ("interestingengineering", "https://interestingengineering.com/rss",         "global",  "tech"),
    ("coolhunting",        "https://coolhunting.com/feed/",                      "global",  "lifestyle"),
    ("dezeen",             "https://www.dezeen.com/feed/",                       "global",  "design"),

    # ── Food + restaurant (8) ──
    ("foodnavigator-usa",  "https://www.foodnavigator-usa.com/rss",              "us",      "food"),
    ("foodnavigator-asia", "https://www.foodnavigator-asia.com/rss",             "asia",    "food"),
    ("foodnavigator-eu",   "https://www.foodnavigator.com/rss",                  "eu",      "food"),
    ("modernrestaurantmgmt", "https://modernrestaurantmanagement.com/feed/",     "us",      "food"),
    ("restaurantbusiness", "https://www.restaurantbusinessonline.com/rss.xml",   "us",      "food"),
    ("qsrmagazine",        "https://www.qsrmagazine.com/rss.xml",                "us",      "food"),
    ("eaterglobal",        "https://www.eater.com/rss/index.xml",                "us",      "food"),
    ("foodandwine",        "https://www.foodandwine.com/feeds/all/rss.xml",      "us",      "food"),

    # ── Fashion + retail (6) ──
    ("voguebusiness",      "https://www.voguebusiness.com/feed",                 "global",  "fashion"),
    ("businessoffashion",  "https://www.businessoffashion.com/arc/outboundfeeds/rss/", "global", "fashion"),
    ("retaildive",         "https://www.retaildive.com/feeds/news/",             "us",      "retail"),
    ("retailweek",         "https://www.retail-week.com/rss",                    "uk",      "retail"),
    ("internetretailing",  "https://internetretailing.net/feed/",                "uk",      "retail"),
    ("retailasia",         "https://retailasia.com/rss.xml",                     "asia",    "retail"),

    # ── Beauty + wellness (4) ──
    ("glossy",             "https://www.glossy.co/feed/",                        "global",  "beauty"),
    ("cosmeticbusiness",   "https://www.cosmeticsbusiness.com/news/rss",         "uk",      "beauty"),
    ("beautyindependent",  "https://www.beautyindependent.com/feed/",            "us",      "beauty"),
    ("wellandgood",        "https://www.wellandgood.com/feed/",                  "us",      "wellness"),

    # ── Healthcare + medtech (5) ──
    ("mobihealthnews",     "https://www.mobihealthnews.com/feed/all",            "global",  "healthtech"),
    ("medcitynews",        "https://medcitynews.com/feed/",                      "us",      "healthtech"),
    ("healthcareitnews",   "https://www.healthcareitnews.com/rss.xml",           "us",      "healthtech"),
    ("medgadget",          "https://www.medgadget.com/feed",                     "global",  "medtech"),
    ("statnews",           "https://www.statnews.com/feed/",                     "us",      "healthtech"),

    # ── Fintech + finance (5) ──
    ("finextra",           "https://www.finextra.com/rss/headlines.aspx",        "global",  "fintech"),
    ("fintechnews-asia",   "https://fintechnews.sg/feed/",                       "sea",     "fintech"),
    ("fintechmagazine",    "https://fintechmagazine.com/feed",                   "global",  "fintech"),
    ("americanbanker",     "https://www.americanbanker.com/feed",                "us",      "fintech"),
    ("paymentssource",     "https://www.paymentssource.com/feed",                "us",      "fintech"),

    # ── EV / mobility / transport (4) ──
    ("electrek",           "https://electrek.co/feed/",                          "global",  "ev"),
    ("insideevs",          "https://insideevs.com/rss/articles/all/",            "global",  "ev"),
    ("greencarcongress",   "https://www.greencarcongress.com/index.rdf",         "global",  "ev"),
    ("transportup",        "https://transportup.com/feed/",                      "global",  "transport"),

    # ── Travel + hospitality (3) ──
    ("skift",              "https://skift.com/feed/",                            "global",  "travel"),
    ("phocuswire",         "https://www.phocuswire.com/RSS",                     "global",  "travel"),
    ("hotelnewsresource",  "https://www.hotelnewsresource.com/rss.xml",          "global",  "travel"),

    # ── Gaming + esports (3) ──
    ("gamesindustrybiz",   "https://www.gamesindustry.biz/feed",                 "global",  "gaming"),
    ("polygon-business",   "https://www.polygon.com/rss/index.xml",              "us",      "gaming"),
    ("eurogamer",          "https://www.eurogamer.net/feed",                     "eu",      "gaming"),

    # ── Crypto + web3 (3) ──
    ("coindesk",           "https://www.coindesk.com/arc/outboundfeeds/rss/",    "global",  "crypto"),
    ("theblock",           "https://www.theblock.co/rss.xml",                    "global",  "crypto"),
    ("decrypt",            "https://decrypt.co/feed",                            "global",  "crypto"),

    # ── Real estate + proptech (3) ──
    ("therealdeal",        "https://therealdeal.com/feed/",                      "us",      "realestate"),
    ("inmancrunch",        "https://www.inman.com/feed/",                        "us",      "realestate"),
    ("propertyinvestortoday", "https://www.propertyinvestortoday.co.uk/rss",     "uk",      "realestate"),

    # ── Regional (foreign markets) ──
    # Asia - China
    ("36kr-en",            "https://36kr.com/feed",                              "cn",      "tech"),
    ("technode",           "https://technode.com/feed/",                         "cn",      "tech"),
    ("scmp-tech",          "https://www.scmp.com/rss/2/feed",                    "cn",      "tech"),
    # Asia - Japan
    ("nikkeiasia",         "https://asia.nikkei.com/rss/feed/business",          "jp",      "general"),
    ("japantimesbiz",      "https://www.japantimes.co.jp/news_category/business/feed/", "jp", "general"),
    # Asia - Korea
    ("koreabizwire",       "https://koreabizwire.com/feed/",                     "kr",      "general"),
    ("mobiinside",         "https://www.mobiinside.co.kr/feed/",                 "kr",      "tech"),
    # Asia - India
    ("yourstory",          "https://yourstory.com/feed",                         "in",      "tech"),
    ("inc42",              "https://inc42.com/feed/",                            "in",      "tech"),
    ("entrackr",           "https://entrackr.com/feed",                          "in",      "tech"),
    # Asia - Southeast Asia
    ("e27",                "https://e27.co/feed/",                               "sea",     "tech"),
    ("tech-in-asia",       "https://www.techinasia.com/feed",                    "sea",     "tech"),
    # LATAM
    ("contxto",            "https://www.contxto.com/en/feed/",                   "latam",   "tech"),
    ("latamlist",          "https://latamlist.com/feed/",                        "latam",   "tech"),
    # Africa
    ("disrupt-africa",     "https://disrupt-africa.com/feed/",                   "africa",  "tech"),
    ("techcabal",          "https://techcabal.com/feed/",                        "africa",  "tech"),
    # MENA
    ("wamda",              "https://www.wamda.com/rss",                          "mena",    "tech"),
    ("menabytes",          "https://www.menabytes.com/feed/",                    "mena",    "tech"),
    # EU
    ("sifted",             "https://sifted.eu/feed/",                            "eu",      "tech"),
    ("eustartups",         "https://www.eu-startups.com/feed/",                  "eu",      "tech"),
    ("techeu",             "https://tech.eu/feed/",                              "eu",      "tech"),
    # AU/NZ
    ("smartcompany",       "https://www.smartcompany.com.au/feed/",              "au",      "general"),

    # ── Investment trend (where money flows) ──
    ("a16z",               "https://a16z.com/feed/",                             "global",  "vc-thesis"),
    ("sequoia-blog",       "https://www.sequoiacap.com/feed/",                   "global",  "vc-thesis"),
    ("benedict-evans",     "https://www.ben-evans.com/benedictevans?format=rss", "global",  "tech-trend"),
    ("notboring",          "https://www.notboring.co/feed",                      "global",  "biz-thinking"),
    ("stratechery-free",   "https://stratechery.com/feed/",                      "global",  "biz-thinking"),

    # ── Newsletter aggregators (industry trend mass) ──
    ("morningbrew",        "https://www.morningbrew.com/daily/feed",             "us",      "biz-news"),
    ("axiosbusiness",      "https://api.axios.com/feed/business",                "us",      "biz-news"),

    # ── Consumer demand / shopping trend ──
    ("etsy-trending",      "https://www.etsy.com/featured?ref=feed",             "global",  "consumer"),
    ("amazon-movers",      "https://www.amazon.com/gp/movers-and-shakers",       "us",      "consumer"),
]


def fetch_rss(name: str, url: str, region: str, cat: str) -> list[dict]:
    raw = _http_get(url, timeout=15)
    return _parse_rss(raw, f"trend:{name}", region=region, cat=cat) if raw else []


# ── Reddit-based trend signals (alt-data) ────────────────────────────
TREND_REDDIT = [
    ("Trendies",          "global", "general"),
    ("Anticonsumption",   "global", "consumer"),
    ("BuyItForLife",      "global", "consumer"),
    ("Frugal",            "global", "consumer"),
    ("foodbusiness",      "global", "food"),
    ("Coffee",            "global", "food"),
    ("FoodIndustryNews",  "global", "food"),
    ("smallbusiness",     "global", "general"),
    ("Entrepreneur",      "global", "general"),
    ("EuropeBiz",         "eu",     "general"),
    ("japanbusiness",     "jp",     "general"),
]


def fetch_reddit_trend(sub: str, region: str, cat: str) -> list[dict]:
    url = f"https://www.reddit.com/r/{sub}/top.json?t=week&limit={MAX_PER_SRC}"
    raw = _http_get(url, timeout=10)
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
        url = "https://www.reddit.com" + p.get("permalink", "")
        posts.append({
            "title": title[:500],
            "body": body[:6000],
            "url": url,
            "score": int(p.get("score") or 0),
            "source": f"trend-reddit:{sub}",
            "region": region,
            "category_hint": cat,
        })
    return posts


# ── HN top trend stories ─────────────────────────────────────────────
def fetch_hn_top_trend() -> list[dict]:
    """HN top stories that are NOT just dev/tech (filter for biz/trend)."""
    url = "https://hacker-news.firebaseio.com/v0/topstories.json"
    raw = _http_get(url, timeout=10)
    if not raw:
        return []
    try:
        ids = json.loads(raw)[:30]
    except Exception:
        return []
    posts = []
    biz_keywords = ["startup", "business", "trend", "consumer", "market",
                    "industry", "company", "raised", "billion", "billion",
                    "growing", "growth", "founder"]
    for hid in ids:
        if _stop or len(posts) >= MAX_PER_SRC:
            break
        try:
            req = urllib.request.Request(
                f"https://hacker-news.firebaseio.com/v0/item/{hid}.json",
                headers={"User-Agent": _ua()},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                item = json.loads(r.read())
        except Exception:
            continue
        if not item or item.get("dead"):
            continue
        title = (item.get("title") or "").strip()
        if len(title) < MIN_TITLE_LEN:
            continue
        if not any(k in title.lower() for k in biz_keywords):
            continue
        url = item.get("url") or f"https://news.ycombinator.com/item?id={hid}"
        posts.append({
            "title": title[:500],
            "body": (item.get("text") or "")[:3000],
            "url": url,
            "score": int(item.get("score") or 0),
            "source": "trend:hn-biz",
            "region": "global",
            "category_hint": "general",
        })
        time.sleep(0.3)
    return posts


# ── orchestration ────────────────────────────────────────────────────
SOURCES = (
    [(name, lambda u=url, n=name, r=region, c=cat:
      fetch_rss(n, u, r, c))
     for name, url, region, cat in RSS_SOURCES]
    + [(f"reddit-trend:{s}",
        lambda s=s, r=region, c=cat: fetch_reddit_trend(s, r, c))
       for s, region, cat in TREND_REDDIT]
    + [("hn-biz", fetch_hn_top_trend)]
)


def make_item(p: dict) -> dict:
    """Trend item — destined for trend-raw queue, NOT validator (different chain)."""
    item_id = (
        f"trend-{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
        f"{p['source'].replace(':', '-').replace('/', '-')}-"
        f"{_fp(p['url'])}"
    )
    return {
        "id": item_id,
        "trace_id": new_trace_id(),
        "discovery_id": item_id,
        "stage": "trend-raw",
        "track": "C",  # TRACK C = global-trend → Thai arbitrage
        "post": {
            "title": p["title"],
            "body": p.get("body", ""),
            "url": p["url"],
            "score": p.get("score", 0),
            "source": p["source"],
        },
        "trend_meta": {
            "region": p.get("region", "global"),
            "category_hint": p.get("category_hint", "general"),
        },
        "history": [{
            "stage": "global-trends",
            "actor": "global-trends",
            "output": (f"emit (region={p.get('region')}, "
                       f"cat={p.get('category_hint')}, "
                       f"src={p['source']})"),
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {
            "text": (f"[{p['source']} | {p.get('region')} | "
                     f"{p.get('category_hint')}] {p['title']}\n\n"
                     f"{(p.get('body') or '')[:1500]}"),
        },
    }


def main() -> int:
    log("global-trends",
        f"streaming {len(SOURCES)} global-trend sources "
        f"(req-gap={PER_REQ_GAP_SEC}s, cycle={CYCLE_GAP_SEC}s) "
        f"→ trend-raw queue (TRACK C)")

    while not _stop:
        cycle_start = time.time()
        emitted = 0
        skipped = 0
        crashed = 0
        for name, fetcher in SOURCES:
            if _stop:
                break
            try:
                posts = fetcher()
            except Exception as e:
                crashed += 1
                log("global-trends",
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
                    write_item(item, "trend-raw")
                    mark_now.append(fp)
                    emitted += 1
                    if emitted <= 3 or emitted % 25 == 0:
                        log("global-trends",
                            f"  ✓ {p['source']} [{p.get('region')}/"
                            f"{p.get('category_hint')}]: "
                            f"{p['title'][:65]}")
                except Exception as e:
                    log("global-trends",
                        f"  ✗ write: {type(e).__name__}: {str(e)[:60]}")
            if mark_now:
                _cf_seen_mark(mark_now)
            time.sleep(PER_REQ_GAP_SEC)
        elapsed = time.time() - cycle_start
        log("global-trends",
            f"cycle done — emitted={emitted}, skipped={skipped}, "
            f"crashed={crashed}, sources={len(SOURCES)}, "
            f"elapsed={elapsed:.1f}s")
        remaining = max(0, CYCLE_GAP_SEC - elapsed)
        for _ in range(int(remaining)):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
