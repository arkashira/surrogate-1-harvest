#!/usr/bin/env python3
"""axentx betalist-stream — pulls upcoming/launched products from BetaList
RSS. Pre-launch products = future competitors + early signal of where
the market is heading.
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

POLL_SEC = int(os.environ.get("BETALIST_POLL_SEC", "10800"))
SEEN_FILE = REPO_ROOT / "state" / "betalist-stream.seen.json"
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (compatible; axentx-discovery/1.0)"
FEED = "https://betalist.com/feed"


_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


EXTRACT_PROMPT = """BetaList pre-launch entry:

Title: {title}
URL: {url}
Snippet: {snippet}

Output STRICT JSON:
{{
  "product_name": "name",
  "pain": "1-sentence pain they target",
  "audience": "specific buyer",
  "monetization": "subscription|usage|enterprise|free|none",
  "monetization_signal": "low|medium|high",
  "pricing_guess": "$X/mo or 'unknown'",
  "axentx_adjacent_idea": "1-sentence ADJACENT product (different niche, same model) or null",
  "tam_signal": "low|medium|high"
}}
"""


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
                        if desc else "")[:1000],
        })
    return items


def _extract(art):
    try:
        out = call_llm(
            EXTRACT_PROMPT.format(**art),
            system="You analyze pre-launch products on BetaList.",
            max_tokens=350, timeout=30,
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
               f"betalist-{h}")
    item = {
        "id": item_id, "trace_id": new_trace_id(), "discovery_id": item_id,
        "stage": "validator", "source": "betalist",
        "url": art["url"], "title": art["title"],
        "pain_one_liner": (sigs.get("axentx_adjacent_idea")
                           or sigs.get("pain", ""))[:240],
        "audience": sigs.get("audience", ""),
        "monetization": sigs.get("monetization", ""),
        "monetization_signal": sigs.get("monetization_signal", "low"),
        "pricing_signal": sigs.get("pricing_guess", ""),
        "tam_signal": sigs.get("tam_signal", "low"),
        "axentx_idea": sigs.get("axentx_adjacent_idea") or "",
        "competitor_name": sigs.get("product_name", ""),
        "raw_signals": sigs,
        "authority_score": 0.7,
        "history": [{
            "stage": "research", "actor": "betalist-stream",
            "output": (f"betalist: {art['title'][:80]} | "
                       f"pain={sigs.get('pain','')[:120]}"),
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
    xml = _http_get(FEED)
    if not xml:
        return False
    for art in _parse_rss(xml):
        if _stop:
            break
        h = hashlib.sha1(art["url"].encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        new_count += 1
        sigs = _extract(art)
        if not sigs:
            continue
        mon = (sigs.get("monetization_signal") or "").lower()
        has_idea = bool(sigs.get("axentx_adjacent_idea"))
        if mon in ("medium", "high") or has_idea:
            _emit(art, sigs)
            emitted += 1
            log("betalist-stream", f"  ✓ {art['title'][:60]} → validator")
        time.sleep(1)
    try:
        SEEN_FILE.write_text(json.dumps(sorted(seen)[-3000:]))
    except Exception:
        pass
    log("betalist-stream", f"cycle: {new_count} new, emitted {emitted}")
    return new_count > 0


if __name__ == "__main__":
    daemon_loop("betalist-stream", POLL_SEC, do_one)
