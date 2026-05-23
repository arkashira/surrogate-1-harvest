#!/usr/bin/env python3
"""axentx substack-stream — pulls Substack newsletters covering startup,
SaaS, growth, PM. Each article = potential idea + monetization pattern.

Newsletters tracked (configurable via SUBSTACK_FEEDS env):
  - Lenny's Newsletter (PM/growth)
  - Starter Story (Pat Walls — niche revenue case studies)
  - First Round Review
  - SaaStr blog (RSS)
  - Substack tag pages: /search/startup, /search/saas
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
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
from axentx_pipeline import (log, call_llm, write_item, daemon_loop,  # noqa: E402
                             new_trace_id)

POLL_SEC = int(os.environ.get("SUBSTACK_POLL_SEC", "3600"))
SEEN_FILE = REPO_ROOT / "state" / "substack-stream.seen.json"
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (compatible; axentx-discovery/1.0)"

DEFAULT_FEEDS = [
    "https://www.lennysnewsletter.com/feed",
    "https://newsletter.starterstory.com/feed",
    "https://review.firstround.com/rss",
    "https://www.saastr.com/feed/",
    "https://every.to/feed.xml",
    "https://stratechery.com/feed/",
    "https://newsletter.pragmaticengineer.com/feed",
    "https://www.notboring.co/feed",
]
FEEDS = os.environ.get("SUBSTACK_FEEDS", ",".join(DEFAULT_FEEDS)).split(",")

EXTRACT_SYSTEM = (
    "You are reading a startup/SaaS/growth newsletter. Extract the actionable "
    "idea + monetization pattern + adjacent-niche opportunity for axentx to "
    "spawn. Skip articles that are pure opinion / no monetizable insight."
)

EXTRACT_PROMPT = """Newsletter article:

{article}

Output STRICT JSON:
{{
  "core_insight": "1-sentence what THIS article teaches about building $$$",
  "audience": "specific who",
  "monetization_pattern": "what monetization model they describe",
  "monetization_signal": "low|medium|high",
  "axentx_idea": "1-sentence ADJACENT product axentx could spawn or null",
  "pricing_hint": "$X/seat/mo guess for the adjacent product",
  "tam_signal": "low|medium|high",
  "skip_reason": "if not monetizable insight: 1 sentence; else null"
}}
"""


_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))
signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("_stop", True))


def _seen_load():
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        return set()


def _seen_save(s):
    try:
        SEEN_FILE.write_text(json.dumps(sorted(s)[-5000:]))
    except Exception:
        pass


def _http_get(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _parse_rss(xml):
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
        chunk = m.group(1)
        title = re.search(
            r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>",
            chunk, re.DOTALL)
        link = re.search(r"<link>(.*?)</link>", chunk)
        desc = re.search(
            r"<description>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</description>",
            chunk, re.DOTALL)
        if not (title and link):
            continue
        items.append({
            "title": re.sub(r"<[^>]+>", "", title.group(1)).strip(),
            "url": link.group(1).strip(),
            "snippet": (re.sub(r"<[^>]+>", " ", desc.group(1))
                        if desc else "")[:1500],
        })
    return items


def _extract(art):
    full = (f"Title: {art['title']}\nURL: {art['url']}\n\n"
            f"Snippet:\n{art.get('snippet','')}")
    try:
        out = call_llm(EXTRACT_PROMPT.format(article=full),
                       system=EXTRACT_SYSTEM, max_tokens=400, timeout=30)
    except Exception:
        return None
    txt = out.strip()
    if "```" in txt:
        seg = txt.split("```", 2)
        if len(seg) >= 2:
            txt = seg[1]
            if txt.startswith("json"):
                txt = txt[4:]
            txt = txt.strip()
    try:
        return json.loads(txt)
    except Exception:
        return None


def _emit(art, sigs):
    h = hashlib.sha1(art["url"].encode()).hexdigest()[:14]
    item_id = (f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
               f"sub-{h}")
    item = {
        "id": item_id, "trace_id": new_trace_id(), "discovery_id": item_id,
        "stage": "validator", "source": "substack",
        "url": art["url"], "title": art["title"],
        "pain_one_liner": (sigs.get("axentx_idea")
                           or sigs.get("core_insight", ""))[:240],
        "audience": sigs.get("audience", ""),
        "monetization": sigs.get("monetization_pattern", ""),
        "monetization_signal": sigs.get("monetization_signal", "low"),
        "pricing_signal": sigs.get("pricing_hint", ""),
        "tam_signal": sigs.get("tam_signal", "low"),
        "axentx_idea": sigs.get("axentx_idea") or "",
        "raw_signals": sigs,
        "authority_score": 0.75,
        "history": [{
            "stage": "research", "actor": "substack-stream",
            "output": (f"substack: {art['title'][:80]} | "
                       f"insight={sigs.get('core_insight','')[:120]}"),
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    write_item(item, "validator")


def do_one():
    seen = _seen_load()
    new_count = emitted = 0
    for feed in FEEDS:
        if _stop:
            break
        feed = feed.strip()
        if not feed:
            continue
        xml = _http_get(feed)
        if not xml:
            continue
        for art in _parse_rss(xml)[:5]:
            h = hashlib.sha1(art["url"].encode()).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            new_count += 1
            sigs = _extract(art)
            if not sigs:
                continue
            mon = (sigs.get("monetization_signal") or "").lower()
            if mon not in ("medium", "high") and not sigs.get("axentx_idea"):
                continue
            _emit(art, sigs)
            emitted += 1
            log("substack-stream", f"  ✓ {art['title'][:60]} → validator")
            time.sleep(1)
        time.sleep(2)
    _seen_save(seen)
    log("substack-stream", f"cycle: {new_count} new, emitted {emitted}")
    return new_count > 0


if __name__ == "__main__":
    daemon_loop("substack-stream", POLL_SEC, do_one)
