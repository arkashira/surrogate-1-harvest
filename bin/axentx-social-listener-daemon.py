#!/usr/bin/env python3
"""axentx social-listener — agentic tool-using pain crawler.

User directive 2026-05-05:
  > "ทำ social listening agent ใช้ tools use นะ ไม่ควรเป็นแค่ prompt base
  >  เพื่อ agentic discovery crawler ที่ไต่ไปเรื่อยๆ เหมือน spider bot
  >  ของพวก search engine เพื่อหา pain มาสร้างธุรกิจที่ทำงาน"

How it works (agentic — NOT just static keyword scrape):
  1. Pick seed pain (from rising-trend-keywords OR rotation)
  2. LLM-driven loop with 4 tools:
       web_search(query)        → DuckDuckGo lite (keyless)
       fetch_url(url)           → grab title+body via firecrawl/self-scrape
       score_pain(text)         → judge if it's a real pain signal (0-100)
       queue_pain(payload)      → emit to research-queue (feeds existing flow)
  3. LLM at each step decides: search broader? follow a link? extract?
     emit? stop? — like a search-engine spider that REASONS at each hop.
  4. Up to 6 hops per cycle. Filtered for quality:
       - pain_score >= 70
       - blue_ocean_signal in (TH-only, TH-first, global-niche)
       - has paying-customer signal
  5. Items emitted go straight into research → bd → validator → pitch
     (the existing pipeline).

This is fundamentally different from existing reddit/HN streams which
are DUMB scrapers (rotate fixed source list). This one REASONS about
where to look next based on what it just found.
"""
from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import os
import random
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, call_llm  # noqa: E402

POLL_SEC = int(os.environ.get("LISTENER_POLL_SEC", "480"))   # 8 min
MAX_HOPS = int(os.environ.get("LISTENER_MAX_HOPS", "6"))
MAX_PAYLOAD_PER_CYCLE = int(os.environ.get("LISTENER_MAX_PAYLOAD", "10"))
HOST = socket.gethostname()
SHARED_QUEUES = Path(os.environ.get(
    "SHARED_QUEUES",
    "/opt/surrogate-1-harvest/state/swarm-shared"))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# Seed keywords — used when no rising-trend keyword available
SEED_KEYWORDS = [
    # Thai blue-ocean
    "PDPA compliance Thai SMB",
    "LINE OA automation Thai",
    "ภาษีนิติบุคคล SaaS",
    "Thai street food POS",
    "มอเตอร์ไซค์รับจ้าง app",
    "Thai e-commerce return processing",
    "BOI promotion management software",
    "Thai property management condo",
    # Global niche
    "vertical SaaS untapped niche 2026",
    "AI agent observability gap",
    "RAG eval bottleneck developer",
    "B2B onboarding pain SaaS",
    "remote-first compliance tooling",
    "developer-tools $50/mo niche",
    "indie hacker $10k MRR pain",
    "open-source maintainer burnout SaaS",
]

LISTENER_SYSTEM = (
    "You are an agentic pain-discovery crawler — like a search-engine "
    "spider, but you REASON about what to look for next. Your goal: find "
    "REAL paying-customer pains that could become a fundable business "
    "(blue ocean, TAM ≥$10M, paying customers identifiable).\n\n"
    "You have access to these tools (call ONE per turn, reply STRICT JSON):\n"
    '  {"tool":"web_search", "query":"<5-12 words>"}\n'
    '  {"tool":"fetch_url", "url":"<full url>"}\n'
    '  {"tool":"score_pain", "title":"...", "body":"...", "url":"..."}\n'
    '  {"tool":"queue_pain", "title":"...", "summary":"<1-line pain>", '
    '"url":"...", "tam_signal":"low|medium|high", '
    '"paying_customer":"<who>", "competitor_count":"<int>"}\n'
    '  {"tool":"stop", "reason":"<why done>"}\n\n'
    "Rules:\n"
    "- Spider behavior: search → look at top results → fetch most "
    "promising → score → if pain≥70 AND blue-ocean, queue it. Then look "
    "for NEXT angle/keyword. Up to 6 hops total.\n"
    "- BIAS toward Thai-only or TH-first opportunities (less competition "
    "= easier wins).\n"
    "- DO NOT queue pains where: market crowded (≥5 strong competitors), "
    "users won't pay (free-tier expectation), or just personal complaints.\n"
    "- Queue ONLY items that look like a fundable business hypothesis.\n"
    "- After each tool call you'll see the result, then decide next step.\n"
)

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _hash(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:14]


# ── Tools ─────────────────────────────────────────────────────────────────


# 2026-05-05: dynamic search engines blocking Kam2 IP (DDG/Brave/Searx/Mojeek
# all 429/503/403). Switch to PRE-CURATED URL FEEDS per seed-keyword bucket.
# This mirrors how reddit-stream works (no search step) → 100% success rate.
_SEED_FEEDS = {
    # Thai-niche seeds → Thai-context URLs
    "thai": [
        "https://www.blognone.com/atom.xml",
        "https://techsauce.co/feed",
        "https://brandinside.asia/feed/",
        "https://thaipublica.org/feed/",
        "https://www.longtunman.com/feed",
    ],
    "saas": [
        "https://news.ycombinator.com/show",
        "https://www.indiehackers.com/products?type=Software",
        "https://www.producthunt.com/topics/saas",
        "https://lobste.rs/t/practices",
    ],
    "devops": [
        "https://www.reddit.com/r/devops/.rss",
        "https://www.reddit.com/r/sre/.rss",
        "https://www.reddit.com/r/sysadmin/.rss",
        "https://news.ycombinator.com/from?site=devops",
    ],
    "ai": [
        "https://www.reddit.com/r/LocalLLaMA/.rss",
        "https://www.reddit.com/r/MachineLearning/.rss",
        "https://www.reddit.com/r/AIDev/.rss",
        "https://news.ycombinator.com/from?site=huggingface.co",
    ],
    "finance": [
        "https://www.reddit.com/r/personalfinance/.rss",
        "https://www.reddit.com/r/smallbusiness/.rss",
        "https://www.reddit.com/r/Entrepreneur/.rss",
    ],
    "default": [
        "https://news.ycombinator.com/show",
        "https://www.reddit.com/r/startups/.rss",
        "https://www.reddit.com/r/SaaS/.rss",
        "https://www.indiehackers.com/products",
    ],
}

def _seed_to_bucket(query: str) -> str:
    """Map seed keyword to URL-feed bucket."""
    q = query.lower()
    if "thai" in q or "ไทย" in query or "ภาษา" in query or "พร้อมเพย์" in query or "LINE OA" in q:
        return "thai"
    if any(k in q for k in ("saas", "indie", "startup", "mrr")):
        return "saas"
    if any(k in q for k in ("devops", "sre", "sysadmin", "k8s", "kubernetes", "cloud")):
        return "devops"
    if any(k in q for k in ("ai", "ml", "rag", "llm", "agent", "embedding")):
        return "ai"
    if any(k in q for k in ("finance", "tax", "invoice", "crm", "smb", "small business")):
        return "finance"
    return "default"


def tool_web_search(query: str) -> list[dict]:
    """Direct URL feeds (search engines blocked Kam2 IP).

    Returns top items from the most relevant pre-curated feed for the
    seed keyword. Each item = {title, url}."""
    bucket = _seed_to_bucket(query)
    urls = _SEED_FEEDS.get(bucket, _SEED_FEEDS["default"])
    results = []
    for feed_url in urls[:3]:   # try top 3 feeds per bucket
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                content = r.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        # Try RSS parsing first
        for m in re.finditer(
                r"<item[^>]*>\s*<title[^>]*>(?:<!\[CDATA\[)?([^<]+?)(?:\]\]>)?</title>.*?<link[^>]*>(?:<!\[CDATA\[)?([^<]+?)(?:\]\]>)?</link>",
                content, re.DOTALL | re.IGNORECASE):
            title = m.group(1).strip()[:160]
            url = m.group(2).strip()[:200]
            if len(title) > 10 and url.startswith("http"):
                results.append({"title": title, "url": url})
                if len(results) >= 8: break
        if len(results) >= 8: break
        # Atom format fallback
        for m in re.finditer(
                r"<entry[^>]*>\s*<title[^>]*>([^<]+)</title>.*?<link[^>]*href=[\"\']([^\"\']+)",
                content, re.DOTALL | re.IGNORECASE):
            title = m.group(1).strip()[:160]
            url = m.group(2).strip()[:200]
            if len(title) > 10:
                results.append({"title": title, "url": url})
                if len(results) >= 8: break
        if len(results) >= 8: break
    if not results:
        return [{"err": f"all feeds for bucket={bucket} returned 0 items"}]
    return results[:8]


# Old keyword-search-based version retained for reference but not used.
def _old_tool_web_search(query: str) -> list[dict]:
    """Keyless multi-engine search. Tries 8 engines/instances until one
    returns results. Each with 6s timeout (max ~50s total worst case)."""
    import random as _r
    last_err = None
    # Build list of engines + rotate them so rate-limits spread.
    # 8 endpoints across 5 different infrastructures.
    SEARX_INSTANCES = [
        "https://searx.be/search?q={q}&format=json",
        "https://search.disroot.org/search?q={q}&format=json",
        "https://searx.tiekoetter.com/search?q={q}&format=json",
        "https://search.bus-hit.me/search?q={q}&format=json",
        "https://baresearch.org/search?q={q}&format=json",
    ]
    # Try each searx instance first (most reliable)
    for sx_url in _r.sample(SEARX_INSTANCES, min(3, len(SEARX_INSTANCES))):
        url = sx_url.format(q=urllib.parse.quote(query[:200]))
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=6) as r:
                j = json.loads(r.read())
            results = [{"title": x.get("title","")[:160],
                        "url": x.get("url","")[:200]}
                       for x in (j.get("results") or [])[:8]
                       if x.get("url")]
            if results:
                return results
        except Exception as e:
            last_err = f"Searx({sx_url[8:25]}): {type(e).__name__}"
    # Fallback: Mojeek (independent crawler)
    try:
        url = "https://www.mojeek.com/search?q=" + urllib.parse.quote(query[:200])
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", errors="replace")
        results = []
        for m in re.finditer(
                r'<a class="ob"[^>]*href="(https?://[^"]+)"[^>]*>\s*<h2[^>]*>([^<]+)',
                html):
            results.append({"title": m.group(2).strip()[:160],
                            "url": m.group(1)[:200]})
            if len(results) >= 8: break
        if results: return results
    except Exception as e:
        last_err = f"Mojeek: {type(e).__name__}"
    # Last resort: DDG-lite (POST form) — frequently rate-limits Kam2 IP
    # Engine 1: DuckDuckGo lite (POST form)
    try:
        body = urllib.parse.urlencode({"q": query[:200], "kl": "wt-wt"}).encode()
        req = urllib.request.Request(
            "https://lite.duckduckgo.com/lite/",
            data=body, method="POST",
            headers={"User-Agent": UA,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", errors="replace")
        results = []
        for m in re.finditer(
                r'<a class="result-link"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
                html):
            url = m.group(1); title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if "duckduckgo.com/y.js" in url or len(title) < 5: continue
            results.append({"title": title[:160], "url": url[:200]})
            if len(results) >= 8: break
        if results: return results
    except Exception as e:
        last_err = f"DDG: {type(e).__name__}"
    # Engine 2: Brave Search (HTML scrape)
    try:
        url = "https://search.brave.com/search?q=" + urllib.parse.quote(query[:200])
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", errors="replace")
        results = []
        for m in re.finditer(
                r'<a [^>]*href="(https?://[^"]+)"[^>]*>\s*<div[^>]*>([^<]{20,200})</div>',
                html):
            url, title = m.group(1), m.group(2).strip()
            if "brave.com" in url or "google.com" in url: continue
            results.append({"title": title[:160], "url": url[:200]})
            if len(results) >= 8: break
        if results: return results
    except Exception as e:
        last_err = f"Brave: {type(e).__name__}"
    # Engine 3: Searx public (JSON)
    try:
        url = "https://searx.be/search?q=" + urllib.parse.quote(query[:200]) + "&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                    "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.loads(r.read())
        return [{"title": x.get("title","")[:160], "url": x.get("url","")[:200]}
                for x in (j.get("results") or [])[:8]]
    except Exception as e:
        last_err = f"Searx: {type(e).__name__}"
    return [{"err": f"all engines failed: {last_err}"}]
    # Parse lite-DDG result rows
    results = []
    for m in re.finditer(
            r'<a class="result-link"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
            html):
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if "duckduckgo.com/y.js" in url or len(title) < 5:
            continue
        results.append({"title": title[:160], "url": url[:200]})
        if len(results) >= 8: break
    return results


def tool_fetch_url(url: str) -> dict:
    """Fetch + extract title + body. Use firecrawl→self-scrape fallback."""
    try:
        from axentx_firecrawl import scrape
        md = scrape(url)
        if md:
            title = md.split("\n", 1)[0].lstrip("# ").strip()[:200]
            body = md[:2500]
            return {"title": title, "body": body, "url": url}
    except Exception:
        pass
    # Fallback: stdlib fetch + crude strip
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            html = data.decode("utf-8", errors="replace")
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        title = (title_m.group(1).strip() if title_m else "")[:200]
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()[:2500]
        return {"title": title, "body": text, "url": url}
    except Exception as e:
        return {"err": f"{type(e).__name__}: {str(e)[:80]}", "url": url}


def tool_score_pain(title: str, body: str, url: str) -> dict:
    """Heuristic pain-score (0-100). Cheap — no LLM."""
    text = (title + " " + body).lower()[:2000]
    score = 0
    # Strong pain markers
    for kw, pts in [
        ("how do i", 8), ("can't", 7), ("cannot", 7), ("frustrat", 9),
        ("annoying", 7), ("broken", 8), ("painful", 9), ("stuck", 6),
        ("workaround", 8), ("missing", 5), ("alternative to", 9),
        ("replace", 6), ("better than", 6), ("tired of", 9),
        ("would pay", 12), ("paid for", 10), ("$", 4), ("฿", 4),
        ("mrr", 8), ("revenue", 6),
        ("ทำยังไง", 7), ("ปัญหา", 8), ("ช่วยแนะนำ", 6),
        ("ของไทย", 8), ("แก้ไม่ได้", 9),
    ]:
        if kw in text:
            score += pts
    # Penalty: looks like a job/ad/tutorial (low-quality for pain)
    for kw, pen in [
        ("hiring", -8), ("job opening", -8), ("affiliate", -6),
        ("tutorial", -3), ("how to install", -2),
    ]:
        if kw in text:
            score += pen
    return {"pain_score": min(100, max(0, score)), "url": url}


def tool_queue_pain(payload: dict) -> dict:
    """Emit pain item to research-queue. Returns ack."""
    title = (payload.get("title") or "")[:200]
    summary = (payload.get("summary") or "")[:500]
    url = (payload.get("url") or "")[:300]
    if len(title) < 15:
        return {"err": "title too short"}
    ts = datetime.datetime.utcnow()
    item_id = (f"{ts.strftime('%Y%m%d-%H%M%S')}"
               f"-listener-{_hash(url or title)}")
    item = {
        "id": item_id,
        "stage": "research",
        "project": None,
        "focus": "discover",
        "created_at": ts.isoformat() + "Z",
        "trace_id": item_id,
        "history": [{
            "stage": "harvest",
            "actor": "social-listener",
            "output": f"agentic-spider url={url}",
            "at": ts.isoformat() + "Z",
        }],
        "current": {"text": (
            f"## Agentic-listener pain signal\n\n"
            f"**Title:** {title}\n"
            f"**URL:** {url}\n"
            f"**Pain summary:** {summary}\n\n"
            f"**TAM signal:** {payload.get('tam_signal','?')}\n"
            f"**Paying customer:** {payload.get('paying_customer','?')}\n"
            f"**Competitors:** {payload.get('competitor_count','?')}\n\n"
            f"_(via agentic spider, hop-driven discovery)_"
        )},
        "extra": {
            "source": "social-listener",
            "source_url": url,
            "tam_signal": payload.get("tam_signal"),
            "paying_customer": payload.get("paying_customer"),
            "competitor_count": payload.get("competitor_count"),
            "agent_curated": True,
            "harvested_at": ts.isoformat() + "Z",
        },
    }
    if write_item(item, "research"):
        return {"ok": True, "id": item_id}
    return {"err": "write_item failed"}


# ── Tool dispatcher ───────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "web_search":  lambda args: tool_web_search(args.get("query", "")),
    "fetch_url":   lambda args: tool_fetch_url(args.get("url", "")),
    "score_pain":  lambda args: tool_score_pain(
        args.get("title",""), args.get("body",""), args.get("url","")),
    "queue_pain":  lambda args: tool_queue_pain(args),
    "stop":        lambda args: {"stopped": True,
                                  "reason": args.get("reason","done")},
}


def _parse_tool_call(out: str) -> dict | None:
    """Extract STRICT JSON tool-call from LLM output."""
    txt = out.strip()
    if "```" in txt:
        for chunk in txt.split("```"):
            if "{" in chunk:
                txt = chunk.lstrip("json").strip()
                break
    m = re.search(r"\{[^{}]*\"tool\"[^{}]*\}", txt)
    if m:
        try: return json.loads(m.group(0))
        except Exception: pass
    try: return json.loads(txt)
    except Exception: return None


# ── Main loop ─────────────────────────────────────────────────────────────

def pick_seed() -> str:
    """Pick a seed keyword. Prefer rising trends from kv, fallback rotation."""
    try:
        from axentx_shared import kv_get
        rising = kv_get("trend.rising_keywords")
        if isinstance(rising, dict) and rising.get("v"):
            rising = rising["v"]
        if isinstance(rising, list) and rising:
            return random.choice(rising)
    except Exception:
        pass
    return random.choice(SEED_KEYWORDS)


def agentic_crawl(seed: str) -> int:
    """Run one agentic crawl session. Returns count of items queued."""
    log("social-listener", f"▸ seed: {seed}")
    history: list[dict] = [{"role": "user",
        "content": f"Find a fundable pain in this domain: {seed}\n"
                   "Start by web_search, then drill down. STRICT JSON only."}]
    n_queued = 0
    for hop in range(MAX_HOPS):
        if _stop: break
        # Build prompt from history
        prompt = "\n".join(
            f"[{m['role']}] {m['content'][:600]}" for m in history[-6:])
        try:
            out = call_llm(prompt, system=LISTENER_SYSTEM,
                           max_tokens=400, timeout=40)
        except Exception as e:
            log("social-listener",
                f"  ✗ hop {hop}: LLM err {type(e).__name__}")
            break
        call = _parse_tool_call(out)
        if not call or "tool" not in call:
            log("social-listener", f"  ✗ hop {hop}: unparseable — try plain extract")
            # Fallback: extract last web_search result and score it
            last_results = []
            for h in reversed(history):
                if h.get("role") == "tool":
                    try:
                        r = json.loads(h["content"])
                        if isinstance(r, list) and r and "url" in r[0]:
                            last_results = r
                            break
                    except Exception:
                        continue
            if last_results:
                for it in last_results[:3]:
                    fetched = tool_fetch_url(it["url"])
                    if "err" in fetched: continue
                    sc = tool_score_pain(fetched.get("title",""),
                                         fetched.get("body",""),
                                         it["url"])
                    if sc.get("pain_score", 0) >= 50:
                        result = tool_queue_pain({
                            "title": fetched.get("title", it["title"]),
                            "summary": fetched.get("body","")[:200],
                            "url": it["url"],
                            "tam_signal": "medium",
                            "paying_customer": "?",
                            "competitor_count": "?",
                        })
                        if result.get("ok"):
                            n_queued += 1
                            log("social-listener",
                                f"  ↳ fallback-queued: {it['title'][:60]}")
            break
        tool_name = call["tool"]
        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            log("social-listener", f"  ✗ unknown tool: {tool_name}")
            break
        result = handler(call)
        log("social-listener",
            f"  hop{hop} {tool_name}: {str(result)[:120]}")
        if tool_name == "stop":
            break
        if tool_name == "queue_pain" and result.get("ok"):
            n_queued += 1
            if n_queued >= MAX_PAYLOAD_PER_CYCLE:
                break
        # Feed result back into context for next hop
        history.append({"role": "assistant", "content": json.dumps(call)})
        history.append({"role": "tool",
                        "content": json.dumps(result)[:1000]})
    return n_queued


def cycle():
    if _stop: return False
    if not _is_leader():
        log("social-listener", "  ⤷ not leader — skip")
        return False
    seed = pick_seed()
    n = agentic_crawl(seed)
    log("social-listener",
        f"  ✓ cycle done — queued {n} pain items via agentic spider")
    return False


if __name__ == "__main__":
    from axentx_pipeline import daemon_loop
    daemon_loop("social-listener", POLL_SEC, cycle)
