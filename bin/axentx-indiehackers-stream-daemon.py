#!/usr/bin/env python3
"""axentx indiehackers-stream — pulls IndieHackers posts/products with
revenue stats. IH is a goldmine for paid-SaaS pain because every post
includes MRR + how they got there. Perfect for product-spawner pipeline.

Sources:
  1. https://www.indiehackers.com/products?revenueRange=1000to10000
     — products in $1K-$10K MRR (proven they CAN make money)
  2. https://www.indiehackers.com/posts/category/general
     — community pain + advice posts
  3. RSS feed: https://www.indiehackers.com/posts.rss

Each post → LLM extract: { problem, who-pays, MRR, growth-channel,
  what-could-WE-spawn-similar-for }
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

POLL_SEC = int(os.environ.get("IH_POLL_SEC", "1800"))
SEEN_FILE = REPO_ROOT / "state" / "indiehackers-stream.seen.json"
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
UA = ("Mozilla/5.0 (compatible; axentx-discovery/1.0; "
      "+https://github.com/axentx)")

EXTRACT_SYSTEM = (
    "You are an indie-SaaS analyst. IndieHackers posts often share "
    "real revenue numbers + the pain that solved it. Extract structured "
    "signals so a product-spawner can replicate the model in adjacent "
    "niches. Be specific about $ + audience."
)

EXTRACT_PROMPT = """IndieHackers post:

{post}

Output STRICT JSON:
{{
  "founder_pain": "1-sentence: what pain did THIS founder solve",
  "audience": "specific buyer (not 'businesses' — say 'agency owners' etc)",
  "mrr_or_pricing": "exact $ if mentioned (e.g., '$3K MRR', '$29/mo'), else 'unknown'",
  "monetization": "subscription|usage|one-time|enterprise|ads|none",
  "growth_channel": "SEO|community|outbound|partnerships|product-led|paid|content|none",
  "monetization_signal": "low|medium|high — based on revenue evidence",
  "tam_signal": "low|medium|high",
  "axentx_adjacent_idea": "an ADJACENT niche where the same model could work (1 sentence) — or null if not applicable",
  "axentx_adjacent_idea_pricing": "$ guess for adjacent niche — e.g., '$49/mo for X users'",
  "skip_reason": "if not promising: 1 sentence why; else null"
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
        SEEN_FILE.write_text(json.dumps(sorted(s)[-5000:]))
    except Exception:
        pass


def fetch_rss() -> list[dict]:
    """IndieHackers public RSS — recent posts."""
    url = "https://www.indiehackers.com/posts.rss"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            xml = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log("ih-stream", f"  ✗ rss: {type(e).__name__}: {str(e)[:80]}")
        return []
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
        chunk = m.group(1)
        title = re.search(r"<title>\s*<!\[CDATA\[(.*?)\]\]>\s*</title>",
                          chunk, re.DOTALL)
        if not title:
            title = re.search(r"<title>(.*?)</title>", chunk)
        link = re.search(r"<link>(.*?)</link>", chunk)
        desc = re.search(r"<description>\s*<!\[CDATA\[(.*?)\]\]>", chunk,
                         re.DOTALL)
        if not (title and link):
            continue
        items.append({
            "title": title.group(1).strip(),
            "url": link.group(1).strip(),
            "snippet": (desc.group(1).strip() if desc else "")[:1500],
        })
    return items


def fetch_post_text(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&amp;", "&").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&nbsp;", " "))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:8000]


def extract_signals(post: dict) -> dict | None:
    full = (
        f"Title: {post['title']}\n"
        f"URL: {post['url']}\n\n"
        f"Snippet:\n{post.get('snippet','')}\n\n"
        f"Body:\n{post.get('body','')[:6000]}"
    )
    try:
        out = call_llm(
            EXTRACT_PROMPT.format(post=full),
            system=EXTRACT_SYSTEM, max_tokens=500, timeout=35,
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


def emit(post: dict, signals: dict) -> None:
    item_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
        f"ih-{hashlib.sha1(post['url'].encode()).hexdigest()[:12]}"
    )
    item = {
        "id": item_id,
        "trace_id": new_trace_id(),
        "discovery_id": item_id,
        "stage": "validator",
        "source": "indiehackers",
        "url": post["url"],
        "title": post["title"],
        "pain_one_liner": (signals.get("axentx_adjacent_idea")
                           or signals.get("founder_pain", ""))[:240],
        "audience": signals.get("audience", ""),
        "monetization": signals.get("monetization", ""),
        "monetization_signal": signals.get("monetization_signal", "low"),
        "pricing_signal": (signals.get("axentx_adjacent_idea_pricing")
                           or signals.get("mrr_or_pricing", "")),
        "growth_channel": signals.get("growth_channel", ""),
        "tam_signal": signals.get("tam_signal", "low"),
        "axentx_idea": signals.get("axentx_adjacent_idea") or "",
        "raw_signals": signals,
        "history": [{
            "stage": "research",
            "actor": "ih-stream",
            "output": (f"ih: {post['title'][:80]} | "
                       f"pain={signals.get('founder_pain','')[:140]} | "
                       f"mrr={signals.get('mrr_or_pricing','?')}"),
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    write_item(item, "validator")


def do_one() -> bool:
    seen = load_seen()
    new_count = 0
    emitted = 0
    posts = fetch_rss()
    for p in posts:
        if _stop:
            break
        h = hashlib.sha1(p["url"].encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        new_count += 1
        p["body"] = fetch_post_text(p["url"])
        signals = extract_signals(p)
        if not signals:
            continue
        mon = (signals.get("monetization_signal") or "").lower()
        has_idea = bool(signals.get("axentx_adjacent_idea"))
        if mon in ("medium", "high") or has_idea:
            emit(p, signals)
            emitted += 1
            log("ih-stream",
                f"  ✓ {p['title'][:60]} → validator (mon={mon})")
        time.sleep(1)
    save_seen(seen)
    log("ih-stream", f"cycle: {new_count} new, emitted {emitted}")
    return new_count > 0


if __name__ == "__main__":
    daemon_loop("ih-stream", POLL_SEC, do_one)
