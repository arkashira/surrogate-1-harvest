#!/usr/bin/env python3
"""axentx crunchbase-funding-stream — pulls Series A/B/C funding signals
from public sources (Crunchbase free tier requires login; use these
free alternatives instead):

Sources (no auth):
  - https://news.crunchbase.com/feed/ (public RSS)
  - https://techcrunch.com/category/venture/feed/
  - https://news.ycombinator.com/rss (Show HN + funding posts)
  - https://www.failory.com/feed (failure stories — anti-pattern data)

A Series-A in vertical X = "this niche is fundable". Extract niche +
amount + investor → emit adjacent-niche idea to validator.
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
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, call_llm, write_item, daemon_loop,  # noqa: E402
                             new_trace_id)

POLL_SEC = int(os.environ.get("FUNDING_POLL_SEC", "10800"))
SEEN_FILE = REPO_ROOT / "state" / "crunchbase-funding-stream.seen.json"
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (compatible; axentx-discovery/1.0)"

FEEDS = [
    "https://news.crunchbase.com/feed/",
    "https://techcrunch.com/category/venture/feed/",
    "https://www.failory.com/feed",
    "https://www.eu-startups.com/feed/",
]

EXTRACT_SYSTEM = (
    "You are a competitive intelligence analyst reading funding/news "
    "articles. Identify the niche that just got VC validation. Extract "
    "raise amount, stage, investor, and propose an adjacent-niche product "
    "axentx could spawn (different vertical, same model)."
)

EXTRACT_PROMPT = """News article:

Title: {title}
URL: {url}
Snippet: {snippet}

Output STRICT JSON:
{{
  "company_funded": "company name (or null if not a funding article)",
  "raise_amount": "$XM (e.g., '$5M Series A') or 'unknown'",
  "investor": "lead investor",
  "stage": "seed|series-a|series-b|growth|other",
  "niche": "what the funded company does — specific vertical",
  "axentx_adjacent_niche": "1-sentence adjacent niche we could enter",
  "axentx_idea": "1-sentence axentx product for that adjacent niche, or null",
  "monetization_signal": "high (VC validates) or low if this is a failure-postmortem article",
  "tam_signal": "low|medium|high"
}}
"""


_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _http_get(url, timeout=15):
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
    try:
        out = call_llm(
            EXTRACT_PROMPT.format(**art),
            system=EXTRACT_SYSTEM, max_tokens=400, timeout=30,
        )
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
               f"funding-{h}")
    item = {
        "id": item_id, "trace_id": new_trace_id(), "discovery_id": item_id,
        "stage": "validator", "source": "crunchbase-funding",
        "url": art["url"], "title": art["title"],
        "pain_one_liner": (sigs.get("axentx_idea")
                           or sigs.get("axentx_adjacent_niche", ""))[:240],
        "audience": sigs.get("niche", ""),
        "monetization_signal": sigs.get("monetization_signal", "low"),
        "tam_signal": sigs.get("tam_signal", "medium"),
        "axentx_idea": sigs.get("axentx_idea") or "",
        "competitor_name": sigs.get("company_funded", ""),
        "competitor_funding": (
            f"{sigs.get('raise_amount','?')} {sigs.get('stage','?')} "
            f"({sigs.get('investor','?')})"),
        "raw_signals": sigs,
        "authority_score": 0.8,
        "history": [{
            "stage": "research", "actor": "crunchbase-funding-stream",
            "output": (f"funding: {art['title'][:80]} | "
                       f"raise={sigs.get('raise_amount','?')[:30]}"),
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    write_item(item, "validator")


def do_one():
    try:
        seen = set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        seen = set()
    new_count = emitted = 0
    for feed in FEEDS:
        if _stop:
            break
        xml = _http_get(feed)
        for art in _parse_rss(xml)[:8]:
            h = hashlib.sha1(art["url"].encode()).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            new_count += 1
            sigs = _extract(art)
            if not sigs or not sigs.get("axentx_idea"):
                continue
            _emit(art, sigs)
            emitted += 1
            log("funding-stream",
                f"  ✓ {art['title'][:60]} → validator "
                f"(raise={sigs.get('raise_amount','?')})")
            time.sleep(1)
        time.sleep(2)
    try:
        SEEN_FILE.write_text(json.dumps(sorted(seen)[-3000:]))
    except Exception:
        pass
    log("funding-stream", f"cycle: {new_count} new, emitted {emitted}")
    return new_count > 0


if __name__ == "__main__":
    daemon_loop("funding-stream", POLL_SEC, do_one)
