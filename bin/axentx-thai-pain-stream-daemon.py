#!/usr/bin/env python3
"""axentx Thai-pain-stream — Thai-only pain signal harvester.

Continuous scrape of Thai-language sources to surface pain points that
foreign tools have NOT addressed. Each item flows through the same
research-queue pipeline as Reddit/HN streams.

Sources (rotated):
  - Pantip — top Thai forum (techology, business, finance, jobs rooms)
  - Blognone — Thai tech news + comments
  - Techsauce — Thai startup ecosystem news
  - Thaiware — Thai software/tools blog
  - Mango Zero — Thai tech/lifestyle commentary
  - Thairath Tech — mainstream tech coverage

Approach: scrape RSS / public JSON / HTML where available, all keyless.
Each item becomes a pain-signal in pipeline_items[research-queue], from
which bd-synth + product-synth pick up Thai-specific blue-ocean ideas.

User feedback 2026-05-04:
  > "หา source ให้ broad กว่านี้อีกมาก deep กว่านี้อีกมาก หาทั้ง source
  >  ไทย และทั่วโลก"
"""
from __future__ import annotations

import datetime
import hashlib
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
import gzip
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, new_trace_id, new_item  # noqa: E402

# ── tunables ──────────────────────────────────────────────────────────────
PER_REQ_GAP_SEC = float(os.environ.get("THAI_REQ_GAP_SEC", "8.0"))
CYCLE_GAP_SEC = float(os.environ.get("THAI_CYCLE_GAP_SEC", "60"))
MAX_PER_SOURCE = int(os.environ.get("THAI_MAX_PER_SOURCE", "20"))
MIN_TITLE_LEN = int(os.environ.get("THAI_MIN_TITLE_LEN", "20"))
POLL_SEC = int(os.environ.get("THAI_POLL_SEC", "900"))   # 15 min between full sweeps

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# Thai pain markers — "how to / why / not work / can't / unsolved" in Thai + EN
PAIN_RE = re.compile(
    r"""(ทำยังไง|ทำไม.*ไม่|ปัญหา|ติดปัญหา|แก้ไม่ได้|ไม่รู้จะ|how do|how to|why does|cant|can't|cannot|frustrat|annoying|broken|stuck|fails|workaround|แนะนำ|มีใครรู้|ช่วย.*หน่อย|ใช้.*ได้ไหม|ของไทย.*มี.*ไหม|มีของไทย.*ไหม|alternative.*thai|thai alternative|local alternative|แพง|ค่าใช้จ่าย|ต้นทุน|กำไร|ขาดทุน|ลดค่าใช้จ่าย|too expensive|too costly|cant afford|burning money|ROI|profit margin|cash flow|ช้า|กิน(เวลา|ทรัพยากร)|เสียเวลา|รอนาน|too slow|takes forever|waste of time|inefficient|bottleneck|ภพ\.20|กรมสรรพากร|VAT|PDPA|กฎหมาย|ราชการ|ใบอนุญาต|sec\.thai|sec\.thailand|BOI|ลงทุน|compliance|audit|ISO|SOC2|LINE OA|LINE Notify|พร้อมเพย์|truemoney|rabbit line pay|shopee|lazada|gojek|grab|robinhood|ภาษาไทย|รองรับไทย|i18n|l10n|locale|thai font|OCR ไทย|speech.*thai|scale|scaling|legacy|tech debt|monolith|migration|refactor|แก้โค้ด|รื้อระบบ|missing feature|feature request|wishlist|feedback|would be nice if|wish.*had|need.*plugin|hack|breach|leaked|vulnerability|exploit|ความปลอดภัย|burnout|overwhelmed|stress|มาก|เครียด|competitor|alternative to|switch from|migrate from|replace|cheaper than|better than|ร้านอาหาร|ส้มตำ|ก๋วยเตี๋ยว|street food|ตลาดนัด|คอนโด|condo|airbnb|ทุเรียน|durian|ข้าวหอมมะลิ|jasmine rice|แท็กซี่|มอเตอร์ไซค์รับจ้าง|win|anime|manga|otaku|kpop|k-pop|cosplay|アニメ|マンガ|เกษตร|farming|ปุ๋ย|fertilizer|agtech|smart farm|e-gov|ภาครัฐ|government tool|public sector)""",
    re.IGNORECASE)

# Sources — each entry: (name, url_template_or_callable, parser_fn_name)
# We use RSS/JSON/HTML scrape paths that don't require accounts.
# 2026-05-06: Cleaned source list — removed mango-zero/thaipublica/workpoint/
# thaiware (general news, no pain signals → bd was killing 100%). Added Pantip
# pain-tagged forums (real Thai user complaints).
# 2026-05-06 v2: massive expansion to social-listening level.
# 3x more sources covering Thai social/biz/consumer/investor lens.
SOURCES = [
    # ─── Tech/Dev pain ─────────────────────────────────────────────────
    ("blognone",            "https://www.blognone.com/atom.xml",          "rss_atom"),
    ("blognone-mobile",     "https://www.blognone.com/topics/mobile/atom.xml", "rss_atom"),
    ("blognone-startup",    "https://www.blognone.com/topics/startup/atom.xml", "rss_atom"),
    ("blognone-business",   "https://www.blognone.com/topics/business/atom.xml", "rss_atom"),
    ("blognone-software",   "https://www.blognone.com/topics/software/atom.xml", "rss_atom"),
    # ─── Thai SaaS / business / startup ────────────────────────────────
    ("techsauce",           "https://techsauce.co/feed",                  "rss_feed"),
    ("brandinside",         "https://brandinside.asia/feed/",             "rss_feed"),
    # ─── Pantip pain forums (expanded) ─────────────────────────────────
    ("pantip-suanlumpini",  "https://pantip.com/forum/suanlumpini/feed",  "rss_feed"),
    ("pantip-silom",        "https://pantip.com/forum/silom/feed",        "rss_feed"),
    ("pantip-blueplanet",   "https://pantip.com/forum/blueplanet/feed",   "rss_feed"),
    ("pantip-jatujak",      "https://pantip.com/forum/jatujak/feed",      "rss_feed"),
    ("pantip-mahawaytong",  "https://pantip.com/forum/mahawaytong/feed",  "rss_feed"),
    ("pantip-greenzone",    "https://pantip.com/forum/greenzone/feed",    "rss_feed"),
    ("pantip-klaibaan",     "https://pantip.com/forum/klaibaan/feed",     "rss_feed"),
    ("pantip-sinthorn",     "https://pantip.com/forum/sinthorn/feed",     "rss_feed"),
    ("pantip-cafe",         "https://pantip.com/forum/cafeteria/feed",    "rss_feed"),
    ("pantip-singhapraja",  "https://pantip.com/forum/singhaprajiao/feed", "rss_feed"),
    ("pantip-supercluster", "https://pantip.com/forum/supercluster/feed", "rss_feed"),
    # ─── Thai consumer drama / virality (NEW) ──────────────────────────
    ("drama-addict",        "https://drama-addict.com/feed/",             "rss_feed"),
    # ─── Thai brand/marketing pain (NEW) ───────────────────────────────
    ("marketingoops",       "https://www.marketingoops.com/feed/",        "rss_feed"),
    ("brandbuffet",         "https://www.brandbuffet.in.th/feed",         "rss_feed"),
    ("positioning-mag",     "https://positioningmag.com/feed",            "rss_feed"),
    # ─── Thai investor / financial sentiment (NEW) ─────────────────────
    ("stock2morrow",        "https://stock2morrow.com/feed",              "rss_feed"),
    ("efinancethai",        "https://www.efinancethai.com/rss/index.aspx", "rss_feed"),
    # ─── Thai mainstream biz news (NEW) ────────────────────────────────
    ("thairath-money",      "https://www.thairath.co.th/rss/money.rss",   "rss_feed"),
    ("bangkokpost-biz",     "https://www.bangkokpost.com/rss/data/business.xml", "rss_feed"),
    ("prachachat-biz",      "https://www.prachachat.net/feed",            "rss_feed"),
    ("matichon-biz",        "https://www.matichon.co.th/business/feed",   "rss_feed"),
    # ─── Thai food/F&B/lifestyle (consumer pain hotspots) ──────────────
    ("ryoii",               "https://ryoiifood.com/feed/",                "rss_feed"),
    ("wongnai-news",        "https://www.wongnai.com/feed.rss",           "rss_feed"),
    # ─── Thai government/regulation (BOI, regulatory pain) ─────────────
    ("boi",                 "https://www.boi.go.th/index.php?page=rss&language=th", "rss_feed"),
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


_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _fetch(url: str, timeout: int = 20) -> str | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "th,en-US;q=0.7,en;q=0.3",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError,
            TimeoutError, Exception) as e:
        log("thai-stream", f"  ✗ fetch {url[:60]}: {type(e).__name__}")
        return None


def _strip(html: str) -> str:
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&[a-z#0-9]+;", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def parse_rss_feed(content: str) -> list[dict]:
    """RSS 2.0 — <item><title>...</title><link>...</link><description>"""
    items = []
    for m in re.finditer(
            r"<item[^>]*>(.*?)</item>", content, re.DOTALL | re.IGNORECASE):
        block = m.group(1)
        title_m = re.search(
            r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
            block, re.DOTALL | re.IGNORECASE)
        link_m = re.search(
            r"<link[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>",
            block, re.DOTALL | re.IGNORECASE)
        desc_m = re.search(
            r"<description[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>",
            block, re.DOTALL | re.IGNORECASE)
        if title_m and link_m:
            items.append({
                "title": _strip(title_m.group(1))[:300],
                "link": _strip(link_m.group(1))[:300],
                "body": _strip(desc_m.group(1))[:600] if desc_m else "",
            })
    return items[:MAX_PER_SOURCE]


def parse_rss_atom(content: str) -> list[dict]:
    """Atom 1.0 — <entry><title>...</title><link href="..."/>"""
    items = []
    for m in re.finditer(
            r"<entry[^>]*>(.*?)</entry>", content, re.DOTALL | re.IGNORECASE):
        block = m.group(1)
        title_m = re.search(
            r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
            block, re.DOTALL | re.IGNORECASE)
        link_m = re.search(r'<link[^>]*href=["\']([^"\']+)["\']',
                           block, re.IGNORECASE)
        sum_m = re.search(
            r"<summary[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</summary>",
            block, re.DOTALL | re.IGNORECASE)
        if title_m and link_m:
            items.append({
                "title": _strip(title_m.group(1))[:300],
                "link": link_m.group(1)[:300],
                "body": _strip(sum_m.group(1))[:600] if sum_m else "",
            })
    return items[:MAX_PER_SOURCE]


def parse_html_pantip(content: str) -> list[dict]:
    """Pantip recent topic listing — parse <a href="/topic/NNN" ...>title</a>"""
    items = []
    for m in re.finditer(
            r'<a[^>]*href=["\'](/topic/\d+)["\'][^>]*>([^<]{20,200})</a>',
            content, re.IGNORECASE):
        items.append({
            "title": _strip(m.group(2))[:300],
            "link": "https://pantip.com" + m.group(1),
            "body": "",
        })
    # dedup
    seen = set()
    out = []
    for it in items:
        if it["link"] in seen: continue
        seen.add(it["link"])
        out.append(it)
        if len(out) >= MAX_PER_SOURCE: break
    return out


PARSERS = {
    "rss_feed": parse_rss_feed,
    "rss_atom": parse_rss_atom,
    "html_pantip": parse_html_pantip,
}


def _is_pain(title: str, body: str) -> bool:
    """Pain heuristic. When demand-amplifier flags broaden=True, accept
    everything (downstream is hungry → don't be picky)."""
    if _broaden_active():
        return True
    text = f"{title} {body}"[:1000]
    if PAIN_RE.search(text):
        return True
    if "?" in title or "ไหม" in title or "หรือเปล่า" in title:
        return True
    return False


def _hash(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:14]


def _harvest_source(name: str, url: str, parser_key: str) -> int:
    """Fetch + parse + emit pain items. Returns count emitted."""
    content = _fetch(url)
    if not content:
        return 0
    parser = PARSERS.get(parser_key)
    if not parser:
        return 0
    try:
        items = parser(content)
    except Exception as e:
        log("thai-stream", f"  ✗ parse {name}: {type(e).__name__}")
        return 0
    if not items:
        return 0

    emitted = 0
    for it in items:
        if len(it["title"]) < MIN_TITLE_LEN:
            continue
        if not _is_pain(it["title"], it["body"]):
            continue
        ts = datetime.datetime.utcnow()
        item_id = (f"{ts.strftime('%Y%m%d-%H%M%S')}"
                   f"-th{name[:8]}-{_hash(it['link'])}")
        item = {
            "id": item_id,
            "stage": "research",
            "project": None,
            "focus": "discover",
            "created_at": ts.isoformat() + "Z",
            "trace_id": item_id,
            "history": [{
                "stage": "harvest",
                "actor": "thai-pain-stream",
                "output": f"source={name} url={it['link']}",
                "at": datetime.datetime.utcnow().isoformat() + "Z",
            }],
            "current": {"text": (                f"## Thai pain signal — {name}\n\n"
                f"**Title (TH):** {it['title']}\n"
                f"**URL:** {it['link']}\n\n"
                f"{it['body'][:500]}\n\n"
                f"_(source: {name}, harvested via thai-pain-stream)_"
            )},
            "extra": {
                "source": name,
                "source_url": it["link"],
                "source_lang": "th",
                "harvested_at": datetime.datetime.utcnow().isoformat() + "Z",
            },
        }
        if write_item(item, "research"):
            log("thai-stream",
                f"  ✓ pain ({name}): {it['title'][:80]}")
            emitted += 1
    return emitted


def cycle():
    if _stop:
        return False
    total = 0
    for name, url, parser_key in SOURCES:
        if _stop:
            break
        n = _harvest_source(name, url, parser_key)
        total += n
        time.sleep(PER_REQ_GAP_SEC)
    log("thai-stream", f"  cycle done — {total} pain items from "
                      f"{len(SOURCES)} sources")
    return False


if __name__ == "__main__":
    from axentx_pipeline import daemon_loop
    daemon_loop("thai-stream", POLL_SEC, cycle)
