#!/usr/bin/env python3
"""axentx biz-trend-asia — international biz trend harvester.

Feeds biz-research-queue with cross-border opportunities (China/Japan/Korea/
Vietnam/Global). User vision: "ในจีนมีขาย เครื่องทำอาหารอัตโนมัติแล้ว แต่ในไทย
ยังไม่มี" — find trends abroad that don't exist in Thailand yet.

Source strategy (RSS-only for stability):
  China:     36Kr, Wallstreetcn, Caixin (biz/tech news + product launches)
  Japan:     Nikkei Asia, Japan Times Biz, IT Media Japan
  Korea:     Korea Herald Biz, Pulse News, The Investor
  Vietnam:   VN Investment Review, VnExpress Biz, Vietnam Briefing
  Global:    Springwise, TrendHunter, Cool Hunting, Amazon best-seller RSS,
             ProductHunt Physical, Etsy Trending

Filter: items must hint at trend/popularity/import-arbitrage signal.
Output → biz-research-queue (consumed by biz-pipeline-daemon TRACK B).
"""
import datetime, hashlib, html, json, os, random, re, sys, time
import urllib.error, urllib.parse, urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import write_item, log, daemon_loop, new_item

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/121.0.0.0 Safari/537.36")

# (name, url, lang, region, category)
SOURCES = [
    # ─── 🇨🇳 China — biz/tech/product launches ────────────────────────
    ("36kr",            "https://36kr.com/feed",                     "zh", "china", "tech-biz"),
    ("wallstreetcn",    "https://wallstreetcn.com/sitemap/news.xml", "zh", "china", "biz"),
    ("caixin-biz",      "https://www.caixin.com/rss/topnews.xml",    "zh", "china", "biz"),
    # ─── 🇯🇵 Japan ─────────────────────────────────────────────────────
    ("nikkei-asia",     "https://asia.nikkei.com/rss/feed/nar",      "en", "japan", "biz"),
    ("japantimes-biz",  "https://www.japantimes.co.jp/feed/business/", "en", "japan", "biz"),
    ("itmedia-news",    "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml", "ja", "japan", "tech"),
    # ─── 🇰🇷 Korea ─────────────────────────────────────────────────────
    ("koreaherald-biz", "http://www.koreaherald.com/rss/020000000000.xml", "en", "korea", "biz"),
    ("pulsenews",       "https://pulsenews.co.kr/rss/business.xml",  "en", "korea", "biz"),
    ("theinvestor",     "https://www.theinvestor.co.kr/rss",         "en", "korea", "biz-startup"),
    # ─── 🇻🇳 Vietnam (Asia neighbor — leading indicator for TH) ───────
    ("vnexpress-biz",   "https://vnexpress.net/rss/kinh-doanh.rss",  "vi", "vietnam", "biz"),
    ("vir",             "https://www.vir.com.vn/rss/home.rss",       "en", "vietnam", "biz"),
    ("vietnam-briefing", "https://www.vietnam-briefing.com/news/feed/", "en", "vietnam", "biz"),
    # ─── 🌍 Global trends ─────────────────────────────────────────────
    ("springwise",      "https://www.springwise.com/feed",           "en", "global", "innovation"),
    ("trendhunter",     "https://www.trendhunter.com/rss/category/Lifestyle", "en", "global", "trend"),
    ("coolhunting",     "https://coolhunting.com/feed/",             "en", "global", "trend"),
    ("ph-physical",     "https://www.producthunt.com/feed?category=physical-products", "en", "global", "products"),
    # Etsy trending categories
    ("etsy-trend",      "https://www.etsy.com/featured/atom.xml",    "en", "global", "ecom"),
    # ─── Asian e-commerce / shopping trend signals ────────────────────
    ("retail-asia",     "https://retail-insight-network.com/feed/",  "en", "asia", "retail"),
    ("e27-trends",      "https://e27.co/feed/?category=startup-news", "en", "sea", "startup"),
    # ─── GDELT 2.0 DOC API (Google Jigsaw, 100+ langs, free) ──────────
    # Real-time global news, 15-min updates, no auth needed.
    ("gdelt-trend-product",
     "https://api.gdeltproject.org/api/v2/doc/doc?query=%22trending%20consumer%20product%22&mode=ArtList&format=rss&maxrecords=20",
     "en", "global", "trend-product"),
    ("gdelt-supply-chain-asia",
     "https://api.gdeltproject.org/api/v2/doc/doc?query=%22supply%20chain%22%20Asia&mode=ArtList&format=rss&maxrecords=20",
     "en", "asia", "supply-chain"),
    ("gdelt-import-thailand",
     "https://api.gdeltproject.org/api/v2/doc/doc?query=import%20Thailand%20demand&mode=ArtList&format=rss&maxrecords=20",
     "en", "thailand", "import-demand"),
    ("gdelt-startup-asia",
     "https://api.gdeltproject.org/api/v2/doc/doc?query=startup%20Asia%20launch&mode=ArtList&format=rss&maxrecords=20",
     "en", "asia", "startup-launch"),
    ("gdelt-china-export",
     "https://api.gdeltproject.org/api/v2/doc/doc?query=China%20export%20product&mode=ArtList&format=rss&maxrecords=20",
     "en", "china", "export"),
]

SEEN_FILE = Path("/opt/surrogate-1-harvest/state/biz-trend-asia.seen.json")


def _load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()


def _save_seen(seen, max_keep=10000):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if len(seen) > max_keep:
        seen = set(list(seen)[-max_keep:])
    SEEN_FILE.write_text(json.dumps(list(seen)))


def _fetch(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_feed(content):
    items = []
    if not content:
        return items
    # RSS
    for m in re.finditer(
            r"<item[^>]*>\s*<title[^>]*>(?:<!\[CDATA\[)?([^<]+?)(?:\]\]>)?</title>"
            r".*?<link[^>]*>(?:<!\[CDATA\[)?([^<]+?)(?:\]\]>)?</link>"
            r"(?:.*?<description[^>]*>(?:<!\[CDATA\[)?([^<]+?)(?:\]\]>)?</description>)?",
            content, re.DOTALL | re.IGNORECASE):
        title = html.unescape(m.group(1).strip())[:240]
        url = m.group(2).strip()[:260]
        body = html.unescape(re.sub(r"<[^>]+>", " ", m.group(3) or ""))[:800]
        if len(title) > 10 and url.startswith("http"):
            items.append({"title": title, "url": url, "body": body})
        if len(items) >= 25:
            break
    if items:
        return items
    # Atom
    for m in re.finditer(
            r"<entry[^>]*>\s*<title[^>]*>([^<]+)</title>"
            r".*?<link[^>]*href=[\"']([^\"']+)[\"']"
            r"(?:.*?<summary[^>]*>([^<]+)</summary>)?",
            content, re.DOTALL | re.IGNORECASE):
        title = html.unescape(m.group(1).strip())[:240]
        url = m.group(2).strip()[:260]
        body = html.unescape(re.sub(r"<[^>]+>", " ", m.group(3) or ""))[:800]
        if len(title) > 10 and url.startswith("http"):
            items.append({"title": title, "url": url, "body": body})
        if len(items) >= 25:
            break
    return items


# Keywords hinting at trend/import-arbitrage opportunity
TREND_PATTERNS = re.compile(
    r"(trending|viral|popular|hot|sold[\- ]out|best[\- ]sell|"
    r"craze|booming|skyrocket|surge|rising|new launch|unveil|"
    r"customer demand|consumer demand|growing|exploded|"
    r"gen[\- ]?z|millennial|tiktok|live[\- ]?stream|"
    r"shortage|out of stock|pre[\- ]order|waitlist|"
    r"# 1|number one|top selling|top-rated|"
    r"突破|爆款|火爆|流行|新品|售罄|畅销|抢购|预订|"
    r"流行|人気|話題|新製品|ヒット|完売|"
    r"트렌드|인기|품절|예약|신상|히트|"
    r"xu hướng|hot|bán chạy|mới ra mắt|cháy hàng|"
    r"innovation|disrupt|first[\- ]ever|breakthrough|"
    r"china|japan|korea|vietnam|asia|asian)",
    re.IGNORECASE
)


def has_trend_signal(text):
    return bool(TREND_PATTERNS.search(text))


def do_one():
    seen = _load_seen()
    new_items = 0
    sources = SOURCES[:]
    random.shuffle(sources)
    # 6 sources per cycle (full rotation 19/6 = ~3 cycles = 5-6 min)
    for name, url, lang, region, category in sources[:6]:
        content = _fetch(url, timeout=12)
        if not content:
            continue
        for it in _parse_feed(content):
            sig = hashlib.sha256(it["url"].encode()).hexdigest()[:16]
            if sig in seen:
                continue
            text = f"{it['title']}\n\n{it['body']}"
            if not has_trend_signal(text):
                seen.add(sig)
                continue
            seen.add(sig)
            item = new_item("null", "biz-trend", text)
            item["id"] = (datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S") +
                          f"-{region[:3]}{name[:8]}-{sig}")
            item["source"] = {
                "type": "biz-trend-asia",
                "feed": name,
                "url": it["url"],
                "lang": lang,
                "region": region,
                "category": category,
            }
            item["current"]["text"] = (
                f"## {region.upper()} {name} biz trend signal — {category}\n\n"
                f"**Title:** {it['title']}\n\n"
                f"**Source:** {it['url']}\n\n"
                f"**Region:** {region.upper()} ({lang})\n\n"
                f"{it['body']}\n\n"
                f"---\n"
                f"_TRACK B candidate — international biz opportunity. "
                f"Compare to Thai market for blue-ocean potential._"
            )
            try:
                # Write directly to biz-research-queue (skip TRACK A bd)
                write_item(item, "biz-research")
                new_items += 1
            except Exception as e:
                log("biz-trend-asia", f"  ✗ write_item: {type(e).__name__}: {e}")
        time.sleep(2.5)
    _save_seen(seen)
    if new_items > 0:
        log("biz-trend-asia", f"+{new_items} biz trends from {len(sources[:6])} feeds")
        return True
    return False


if __name__ == "__main__":
    daemon_loop("biz-trend-asia", 120, do_one)
