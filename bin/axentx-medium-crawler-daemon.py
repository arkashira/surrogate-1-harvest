#!/usr/bin/env python3
"""axentx medium-crawler — pulls articles from Medium tags about startups,
SaaS, indie hacking, business — analyzes them, extracts ideas + monetization
patterns, and pushes promising ideas straight into the validator-queue.

User directive 2026-05-04:
  > 'หา agent ไป scape บทความทั้งหมดบน medium ... tag startup,
  >  soloentrepreneur, SaaS, medium startup ideas, business ...
  >  เอา idea ต่างๆ มาสร้างเป็น new product แล้วใช้ chain ของการสร้าง
  >  product ของเรานี่แหละ สร้างจริงให้ได้'

Strategy:
  1. Pull Medium tag RSS feeds (no auth needed, ~10 articles per tag per pull)
  2. Fetch full article content via simple HTTP scrape (HTML → text)
  3. LLM extract: { problem, solution, pricing, growth_channel, monetization }
  4. Filter: only items with explicit monetization (not open-source)
  5. Push to validator queue with full context

Tag list (configurable via MEDIUM_TAGS env, comma-separated):
  startup, saas, solopreneur, indie-hacker, business,
  startup-ideas, micro-saas, side-project, no-code, build-in-public

Daemon uses dedup (URL hash) so the same article isn't re-emitted.
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

POLL_SEC = int(os.environ.get("MEDIUM_POLL_SEC", "1800"))   # 30 min
TAGS = os.environ.get("MEDIUM_TAGS", (
    "startup,saas,solopreneur,indie-hackers,business,"
    "startup-ideas,micro-saas,side-project,no-code,build-in-public,"
    "entrepreneurship,product-management,growth,bootstrapped"
)).split(",")
SEEN_FILE = REPO_ROOT / "state" / "medium-crawler.seen.json"
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

EXTRACT_SYSTEM = (
    "You are a startup analyst. Given a Medium article about a startup, "
    "SaaS, or indie product, extract a STRUCTURED summary that will feed "
    "an autonomous product-spawner pipeline. The pipeline only ships "
    "products with REAL monetization paths — open-source-only ideas are "
    "REJECTED upstream. Be specific, numerical when possible, opinionated."
)

EXTRACT_PROMPT = """Article (title + URL + first 8000 chars):

{article}

Output STRICT JSON (no prose, no markdown):
{{
  "problem": "the user pain in 1 sentence (concrete + scoped)",
  "solution": "what the founder built in 1 sentence",
  "audience": "who pays — be specific (e.g., 'B2B SaaS founders 1-10 employees', not 'businesses')",
  "monetization": "subscription|usage|one-time|enterprise|ads|none",
  "pricing": "actual $/mo or $/user or 'unknown' — extract exact numbers if mentioned",
  "growth_channel": "primary distribution (SEO|community|outbound|partnerships|product-led|paid|content)",
  "tech_stack_hint": "languages/frameworks if mentioned, else 'unknown'",
  "tam_signal": "low|medium|high",
  "monetization_signal": "low|medium|high — how clear is the path to recurring revenue",
  "axentx_idea": "if this article suggests a derivable NEW product idea we could spawn, describe it in 1 sentence; else null",
  "axentx_idea_monetization": "if axentx_idea: subscription|usage|one-time|enterprise; else null",
  "skip_reason": "if monetization=none OR not promising for spawning, 1 sentence why; else null"
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


def save_seen(seen: set) -> None:
    try:
        SEEN_FILE.write_text(json.dumps(sorted(seen)[-5000:]))   # cap 5K
    except Exception:
        pass


def fetch_tag_rss(tag: str) -> list[dict]:
    """Medium tag RSS — public, no auth. Returns articles since last pull."""
    url = f"https://medium.com/feed/tag/{urllib.parse.quote(tag)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            xml = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log("medium-crawler", f"  ✗ tag {tag}: {type(e).__name__}: {str(e)[:80]}")
        return []

    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
        chunk = m.group(1)
        title = re.search(r"<title>\s*<!\[CDATA\[(.*?)\]\]>\s*</title>",
                          chunk, re.DOTALL)
        link = re.search(r"<link>(.*?)</link>", chunk)
        desc = re.search(
            r"<description>\s*<!\[CDATA\[(.*?)\]\]>\s*</description>",
            chunk, re.DOTALL)
        author = re.search(r"<dc:creator>\s*<!\[CDATA\[(.*?)\]\]>",
                           chunk, re.DOTALL)
        if not (title and link):
            continue
        items.append({
            "title": title.group(1).strip(),
            "url": link.group(1).strip(),
            "author": author.group(1).strip() if author else "",
            "snippet": (desc.group(1).strip() if desc else "")[:600],
            "tag": tag,
        })
    return items


def fetch_article_text(url: str) -> str:
    """Fetch + strip Medium article HTML to plain text. Best-effort."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    # Drop script/style blocks
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL)
    # Drop tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common entities
    text = (text.replace("&amp;", "&").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&nbsp;", " ")
                .replace("&lt;", "<").replace("&gt;", ">"))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:12000]


def extract_signals(article: dict) -> dict | None:
    """LLM-extract structured fields from article. Returns None on parse fail."""
    full = (
        f"Title: {article['title']}\n"
        f"URL: {article['url']}\n"
        f"Author: {article.get('author','')}\n"
        f"Tag: {article.get('tag','')}\n\n"
        f"Body:\n{article.get('body','')[:8000]}"
    )
    try:
        out = call_llm(
            EXTRACT_PROMPT.format(article=full),
            system=EXTRACT_SYSTEM, max_tokens=600, timeout=40,
        )
    except Exception as e:
        log("medium-crawler",
            f"  ✗ LLM extract: {type(e).__name__}: {str(e)[:120]}")
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


def emit_to_validator(article: dict, signals: dict) -> None:
    """Push to validator queue with full context. validator → market-research
    → bd → spawn... = the existing chain handles the rest."""
    item_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
        f"medium-{hashlib.sha1(article['url'].encode()).hexdigest()[:12]}"
    )
    item = {
        "id": item_id,
        "trace_id": new_trace_id(),
        "discovery_id": item_id,
        "stage": "validator",
        "source": "medium",
        "url": article["url"],
        "title": article["title"],
        "author": article.get("author", ""),
        "tag": article.get("tag", ""),
        "pain_one_liner": signals.get("problem", "")[:240],
        "audience": signals.get("audience", ""),
        "monetization": signals.get("monetization", ""),
        "monetization_signal": signals.get("monetization_signal", "low"),
        "pricing_signal": signals.get("pricing", ""),
        "growth_channel": signals.get("growth_channel", ""),
        "tam_signal": signals.get("tam_signal", "low"),
        "axentx_idea": signals.get("axentx_idea") or "",
        "axentx_idea_monetization": signals.get("axentx_idea_monetization") or "",
        "raw_signals": signals,
        "history": [{
            "stage": "research",
            "actor": "medium-crawler",
            "output": (f"medium/{article['tag']}: {article['title']} | "
                       f"prob={signals.get('problem','')[:160]} | "
                       f"mon={signals.get('monetization','?')} | "
                       f"pricing={signals.get('pricing','?')[:60]}"),
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    write_item(item, "validator")


def do_one() -> bool:
    """One scan cycle across all configured tags."""
    seen = load_seen()
    new_count = 0
    promising_count = 0
    for tag in TAGS:
        if _stop:
            break
        articles = fetch_tag_rss(tag)
        for a in articles:
            url_hash = hashlib.sha1(a["url"].encode()).hexdigest()
            if url_hash in seen:
                continue
            seen.add(url_hash)
            new_count += 1
            # Fetch + extract
            a["body"] = fetch_article_text(a["url"])
            if not a["body"] or len(a["body"]) < 500:
                continue
            signals = extract_signals(a)
            if not signals:
                continue
            # Filter: only promising (clear monetization OR strong axentx-idea)
            mon = (signals.get("monetization_signal") or "").lower()
            has_idea = bool(signals.get("axentx_idea"))
            if mon in ("medium", "high") or has_idea:
                emit_to_validator(a, signals)
                promising_count += 1
                log("medium-crawler",
                    f"  ✓ {a['tag']}: {a['title'][:60]} → validator "
                    f"(mon={mon}, idea={'yes' if has_idea else 'no'})")
            else:
                log("medium-crawler",
                    f"  · skip {a['tag']}: {a['title'][:50]} "
                    f"(mon={mon}, no axentx-idea)")
        # Be nice: small sleep between tags
        time.sleep(2)
    save_seen(seen)
    log("medium-crawler",
        f"cycle: scanned {new_count} new articles, "
        f"emitted {promising_count} to validator")
    return new_count > 0


if __name__ == "__main__":
    daemon_loop("medium-crawler", POLL_SEC, do_one)
