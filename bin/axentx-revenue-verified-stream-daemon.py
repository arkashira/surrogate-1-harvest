#!/usr/bin/env python3
"""axentx revenue-verified-stream — pulls from sources where companies
publish VERIFIED revenue numbers (Stripe-backed open dashboards). These
are the highest-signal idea sources because the $$$ is real, not claimed.

Sources (rotating each cycle):
  1. https://openstartup.dev/open      — aggregator of open-startup pages
  2. https://openstartuplist.com       — directory of companies with public MRR
  3. https://baremetrics.com/open-startups — live Baremetrics MRR dashboards
  4. https://trustmrr.com/             — Stripe/RevenueCat verified MRR (RSS feed)
  5. https://www.indiehackers.com/products?revenueRange=1000to10000
     — IH products in proven-revenue range

Why this beats just looking at PH/HN/Medium:
  - $$ is verified, not "I think we have 10K MRR"
  - You see what niche actually pays + how much
  - Pricing patterns translate directly to our pricing-tier output
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

POLL_SEC = int(os.environ.get("REVENUE_STREAM_POLL_SEC", "10800"))   # 3h
SEEN_FILE = REPO_ROOT / "state" / "revenue-stream.seen.json"
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

EXTRACT_SYSTEM = (
    "You are a competitive analyst studying open-revenue startup data. "
    "Each entry is a real company with verified MRR. Extract the pattern "
    "and propose an ADJACENT-niche product axentx could spawn — same "
    "monetization model, different vertical/audience. Be specific about $$$"
)

EXTRACT_PROMPT = """Open-startup data point:

{entry}

Output STRICT JSON:
{{
  "company": "company name",
  "verified_mrr": "$X (e.g., '$12.5K MRR') or 'unknown'",
  "niche": "specific niche they serve",
  "pricing_pattern": "$X/seat/mo or $X/usage — concrete",
  "monetization": "subscription|usage|enterprise",
  "growth_channel": "SEO|community|outbound|product-led|content|paid|referral",
  "axentx_adjacent_idea": "1-sentence ADJACENT-niche product (different vertical, same model). null if not applicable",
  "axentx_pricing_guess": "$X-Y/seat/mo for the adjacent niche",
  "monetization_signal": "high (verified MRR) — always high for this source",
  "tam_adjacent": "low|medium|high — how big is the adjacent niche"
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


def _http_get(url: str, timeout: int = 20) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def fetch_openstartuplist() -> list[dict]:
    """openstartuplist.com — HTML directory."""
    html = _http_get("https://openstartuplist.com/")
    if not html:
        return []
    # Find anchor blocks with company names + MRR mentions
    entries = []
    # Heuristic: blocks that contain $X and a link
    blocks = re.findall(
        r"<a[^>]+href=\"([^\"]+)\"[^>]*>([^<]+)</a>[^<]*"
        r"(?:</?[a-z]+[^>]*>\s*)*\$([0-9]+(?:[KMB]|,\d{3})?)",
        html, re.IGNORECASE,
    )
    for url, name, mrr in blocks[:30]:
        entries.append({
            "source": "openstartuplist",
            "company": name.strip(),
            "url": url.strip(),
            "mrr_hint": f"${mrr}",
        })
    return entries


def fetch_trustmrr_rss() -> list[dict]:
    """TrustMRR public RSS — Stripe-verified MRR + niche tags."""
    xml = _http_get("https://trustmrr.com/feed.xml")
    if not xml:
        # try alternate feed paths
        xml = _http_get("https://trustmrr.com/rss")
    if not xml:
        return []
    entries = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
        chunk = m.group(1)
        title = re.search(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>",
                          chunk, re.DOTALL)
        link = re.search(r"<link>(.*?)</link>", chunk)
        desc = re.search(r"<description>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</description>",
                         chunk, re.DOTALL)
        if not (title and link):
            continue
        entries.append({
            "source": "trustmrr",
            "company": re.sub(r"<[^>]+>", "", title.group(1)).strip(),
            "url": link.group(1).strip(),
            "snippet": (re.sub(r"<[^>]+>", " ", desc.group(1))
                        if desc else "")[:1500],
        })
    return entries


def fetch_baremetrics_open() -> list[dict]:
    """Baremetrics public open-startups list — Buffer, Cal.com, etc."""
    html = _http_get("https://baremetrics.com/open-startups")
    if not html:
        return []
    # Find external company links
    entries = []
    for m in re.finditer(
            r"<a[^>]+href=\"(https?://[^\"]+)\"[^>]*>\s*<[^<]+>\s*([^<]{3,50})",
            html, re.IGNORECASE):
        url = m.group(1).strip()
        name = m.group(2).strip()
        if "baremetrics.com" in url or "twitter" in url:
            continue
        entries.append({
            "source": "baremetrics",
            "company": name,
            "url": url,
        })
    return entries[:25]


def fetch_ih_products() -> list[dict]:
    """IndieHackers products list — has MRR per product."""
    # IH products page is JS-rendered; use the JSON-LD or fall back to no-op
    html = _http_get(
        "https://www.indiehackers.com/products?revenueVerification=verified")
    if not html:
        return []
    entries = []
    for m in re.finditer(
            r"<a[^>]+href=\"(/product/[^\"]+)\"[^>]*>([^<]{3,80})", html):
        name = m.group(2).strip()
        if not name or name.lower() in ("home", "products", "more"):
            continue
        entries.append({
            "source": "indiehackers",
            "company": name,
            "url": "https://www.indiehackers.com" + m.group(1),
        })
    return entries[:25]


def extract_signals(entry: dict) -> dict | None:
    full = (
        f"Source: {entry.get('source')}\n"
        f"Company: {entry.get('company')}\n"
        f"URL: {entry.get('url')}\n"
        f"MRR hint: {entry.get('mrr_hint','?')}\n"
        f"Snippet: {entry.get('snippet','')[:1200]}"
    )
    try:
        out = call_llm(
            EXTRACT_PROMPT.format(entry=full),
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


def emit(entry: dict, signals: dict) -> None:
    h = hashlib.sha1((entry.get("company", "") + entry.get("url", ""))
                     .encode()).hexdigest()[:14]
    item_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
        f"rev-{entry.get('source','?')}-{h}"
    )
    item = {
        "id": item_id,
        "trace_id": new_trace_id(),
        "discovery_id": item_id,
        "stage": "validator",
        "source": f"revenue-verified/{entry.get('source','?')}",
        "url": entry.get("url", ""),
        "title": entry.get("company", "?"),
        "pain_one_liner": (signals.get("axentx_adjacent_idea")
                           or f"Adjacent niche to {signals.get('niche','')}")[:240],
        "audience": signals.get("niche", ""),
        "monetization": signals.get("monetization", "subscription"),
        "monetization_signal": "high",   # verified revenue source
        "pricing_signal": signals.get("axentx_pricing_guess", ""),
        "growth_channel": signals.get("growth_channel", ""),
        "tam_signal": signals.get("tam_adjacent", "medium"),
        "axentx_idea": signals.get("axentx_adjacent_idea") or "",
        "competitor_name": signals.get("company", entry.get("company", "")),
        "competitor_mrr": signals.get("verified_mrr", ""),
        "raw_signals": signals,
        "authority_score": 0.85,
        "history": [{
            "stage": "research",
            "actor": "revenue-verified-stream",
            "output": (f"{entry.get('source','?')}: "
                       f"{entry.get('company','')[:60]} "
                       f"({signals.get('verified_mrr','?')[:30]}) → "
                       f"adj={signals.get('axentx_adjacent_idea','')[:120]}"),
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    write_item(item, "validator")


def do_one() -> bool:
    seen = load_seen()
    all_entries = []
    for fetcher in (fetch_openstartuplist, fetch_trustmrr_rss,
                    fetch_baremetrics_open, fetch_ih_products):
        try:
            all_entries.extend(fetcher())
            time.sleep(2)
        except Exception as e:
            log("revenue-stream",
                f"  ✗ {fetcher.__name__}: {type(e).__name__}: {str(e)[:80]}")

    new_count = 0
    emitted = 0
    for e in all_entries:
        if _stop:
            break
        h = hashlib.sha1((e.get("company", "") + e.get("url", ""))
                         .encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        new_count += 1
        signals = extract_signals(e)
        if not signals or not signals.get("axentx_adjacent_idea"):
            continue
        emit(e, signals)
        emitted += 1
        log("revenue-stream",
            f"  ✓ {e.get('source')}: {e.get('company','')[:40]} → "
            f"{signals.get('axentx_adjacent_idea','')[:60]}")
        time.sleep(1)
    save_seen(seen)
    log("revenue-stream",
        f"cycle: {len(all_entries)} fetched, {new_count} new, "
        f"{emitted} emitted")
    return new_count > 0


if __name__ == "__main__":
    daemon_loop("revenue-stream", POLL_SEC, do_one)
