#!/usr/bin/env python3
"""axentx producthunt-stream — pulls trending products from Product Hunt
front page (via public RSS / leaderboard scrape), extracts pain + pricing
+ adjacent-niche idea, emits to validator-queue.

PH is great for spotting momentum + competitor analysis — every launch
exposes the pain they're solving + the pricing tier they think the market
will pay.
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

POLL_SEC = int(os.environ.get("PH_POLL_SEC", "3600"))   # 1 hour
SEEN_FILE = REPO_ROOT / "state" / "producthunt-stream.seen.json"
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

EXTRACT_SYSTEM = (
    "You are a competitive analyst reading Product Hunt launches. For each "
    "product, extract pain + pricing + the adjacent niche we could spawn a "
    "similar product for. Reject ideas without clear monetization."
)

EXTRACT_PROMPT = """Product Hunt launch:

{post}

Output STRICT JSON:
{{
  "product_name": "the product name",
  "tagline": "1-line tagline if available",
  "pain": "the user pain (1 sentence)",
  "audience": "specific buyer",
  "pricing": "$/mo or 'free' or 'unknown' — extract exact",
  "monetization": "subscription|usage|one-time|enterprise|ads|free|none",
  "monetization_signal": "low|medium|high",
  "tam_signal": "low|medium|high",
  "axentx_idea": "1-sentence ADJACENT product we could build (different niche, same model)",
  "axentx_idea_pricing": "$/mo guess for the adjacent niche",
  "skip_reason": "if not promising: 1 sentence; else null"
}}
"""


_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def load_seen() -> set:
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        return set()


def save_seen(s: set) -> None:
    try:
        SEEN_FILE.write_text(json.dumps(sorted(s)[-3000:]))
    except Exception:
        pass


def fetch_ph() -> list[dict]:
    """PH public RSS feed — daily front page leaderboard."""
    url = "https://www.producthunt.com/feed?category=undefined"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            xml = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log("ph-stream", f"  ✗ rss: {type(e).__name__}: {str(e)[:80]}")
        return []
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
        chunk = m.group(1)
        title = re.search(r"<title>\s*<!\[CDATA\[(.*?)\]\]>\s*</title>",
                          chunk, re.DOTALL)
        if not title:
            title = re.search(r"<title>(.*?)</title>", chunk)
        link = re.search(r"<link>(.*?)</link>", chunk)
        desc = re.search(r"<description>\s*<!\[CDATA\[(.*?)\]\]>",
                         chunk, re.DOTALL)
        if not (title and link):
            continue
        items.append({
            "title": title.group(1).strip(),
            "url": link.group(1).strip(),
            "snippet": (desc.group(1).strip() if desc else "")[:1500],
        })
    return items


def extract_signals(post: dict) -> dict | None:
    full = (f"Title: {post['title']}\n"
            f"URL: {post['url']}\n\n"
            f"Description:\n{post.get('snippet','')}")
    try:
        out = call_llm(EXTRACT_PROMPT.format(post=full),
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


def emit(post: dict, signals: dict) -> None:
    item_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
        f"ph-{hashlib.sha1(post['url'].encode()).hexdigest()[:12]}"
    )
    item = {
        "id": item_id,
        "trace_id": new_trace_id(),
        "discovery_id": item_id,
        "stage": "validator",
        "source": "producthunt",
        "url": post["url"],
        "title": post["title"],
        "pain_one_liner": (signals.get("axentx_idea")
                           or signals.get("pain", ""))[:240],
        "audience": signals.get("audience", ""),
        "monetization": signals.get("monetization", ""),
        "monetization_signal": signals.get("monetization_signal", "low"),
        "pricing_signal": (signals.get("axentx_idea_pricing")
                           or signals.get("pricing", "")),
        "tam_signal": signals.get("tam_signal", "low"),
        "axentx_idea": signals.get("axentx_idea") or "",
        "competitor_name": signals.get("product_name", ""),
        "raw_signals": signals,
        "history": [{
            "stage": "research",
            "actor": "ph-stream",
            "output": (f"ph: {post['title'][:80]} | "
                       f"pain={signals.get('pain','')[:140]} | "
                       f"pricing={signals.get('pricing','?')[:60]}"),
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    write_item(item, "validator")


def do_one() -> bool:
    seen = load_seen()
    new_count = 0
    emitted = 0
    for p in fetch_ph():
        if _stop:
            break
        h = hashlib.sha1(p["url"].encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        new_count += 1
        signals = extract_signals(p)
        if not signals:
            continue
        mon = (signals.get("monetization_signal") or "").lower()
        if mon not in ("medium", "high"):
            continue
        emit(p, signals)
        emitted += 1
        log("ph-stream",
            f"  ✓ {p['title'][:60]} → validator (mon={mon})")
        time.sleep(0.5)
    save_seen(seen)
    log("ph-stream", f"cycle: {new_count} new, emitted {emitted}")
    return new_count > 0


if __name__ == "__main__":
    daemon_loop("ph-stream", POLL_SEC, do_one)
