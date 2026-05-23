#!/usr/bin/env python3
"""axentx yc-rfs-tracker — pulls Y Combinator's Requests for Startups
(https://www.ycombinator.com/rfs). YC explicitly publishes thesis-grade
'these are the products we want funded' essays per batch.

User directive 2026-05-04:
  > 'ลองดูเอาแนวคิดของ ที่นี่มาใช้กับเราได้ไหม ... ycombinator.com/rfs'

Why this is the highest-signal source we have:
  - VC-authored: "we will fund this" — pre-validated demand
  - Thesis-grade: 500-1000 words per RFS, not a tweet
  - Free + crawl-friendly: plain HTML, no auth, no anti-bot
  - Refreshes ~2× per year (every YC batch)
  - Recent topics: AI for Agriculture, AI-Native Discovery, AI-Native
    Services, Personalized Medicine, Counter-Swarm Defense, Inference
    Chips for Agents

Pipeline slot: emits to validator-queue with output_mode=paid-product
(YC RFS = high monetization signal by definition — no open-source-only).
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
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, call_llm, write_item, daemon_loop,  # noqa: E402
                             new_trace_id)

POLL_SEC = int(os.environ.get("YC_RFS_POLL_SEC", "21600"))   # 6h
SEEN_FILE = REPO_ROOT / "state" / "yc-rfs.seen.json"
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

EXTRACT_SYSTEM = (
    "You are a senior YC partner translating a Request for Startup into a "
    "structured product hypothesis the axentx pipeline can spawn. The RFS "
    "describes a problem space YC partners want to fund — extract the "
    "concrete product, audience, monetization model, and what would be "
    "the v1 wedge. Be specific, not abstract."
)

EXTRACT_PROMPT = """YC Request for Startup:

{rfs}

Output STRICT JSON (no markdown):
{{
  "rfs_title": "the title of the RFS",
  "yc_partner": "author if available, else 'unknown'",
  "problem": "1-sentence concrete pain — be specific",
  "product_hypothesis": "1-sentence what we'd build for v1",
  "audience": "specific buyer (not 'companies' — say 'mid-market manufacturers' etc)",
  "monetization": "subscription|usage|enterprise|marketplace|none",
  "monetization_signal": "high (this is YC RFS — they only fund $$$ businesses)",
  "tam_signal": "low|medium|high",
  "pricing_guess": "$X-Y/seat/mo or $X-Y/usage — concrete numbers",
  "v1_wedge": "1-sentence what to ship in <90 days that proves the model",
  "axentx_idea": "1-sentence axentx-flavored take on this RFS",
  "incumbent_competitors": ["1-3 named existing companies in this space"],
  "why_now": "why this works in 2026 (1 sentence)"
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
        SEEN_FILE.write_text(json.dumps(sorted(s)))
    except Exception:
        pass


def fetch_rfs_index() -> str:
    """Get the main RFS page HTML."""
    try:
        req = urllib.request.Request(
            "https://www.ycombinator.com/rfs",
            headers={"User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log("yc-rfs", f"  ✗ fetch index: {type(e).__name__}: {str(e)[:80]}")
        return ""


def parse_rfs_entries(html: str) -> list[dict]:
    """Parse the YC RFS page — sections are h2/h3 + body paragraphs.

    YC RFS layout: h2 = category, then individual RFS entries are h3-style
    blocks with title + 1-2 paragraphs of thesis. Use a heuristic: split
    on h2/h3 headings, take title + next sibling text.
    """
    # Strip scripts + styles
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL)

    # Find all h2/h3 blocks with their content until next heading.
    # YC sometimes uses h2 for categories, h3 for individual RFS — we'll
    # treat anything h2/h3 that has substantial body text after it as
    # a candidate RFS.
    entries = []
    pattern = re.compile(
        r"<h([23])[^>]*>(.*?)</h\1>(.*?)(?=<h[23][^>]*>|</main|</body)",
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        title_html = m.group(2)
        body_html = m.group(3)
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        body = re.sub(r"<[^>]+>", " ", body_html)
        body = (body.replace("&nbsp;", " ").replace("&amp;", "&")
                    .replace("&quot;", '"').replace("&#39;", "'")
                    .replace("&rsquo;", "'").replace("&lsquo;", "'")
                    .replace("&ldquo;", '"').replace("&rdquo;", '"'))
        body = re.sub(r"\s+", " ", body).strip()
        if not title or len(body) < 200:
            continue
        # Skip nav / footer noise
        if title.lower() in ("apply", "subscribe", "more", "about", "menu"):
            continue
        entries.append({"title": title, "body": body[:6000]})
    return entries


def extract_signals(entry: dict) -> dict | None:
    full = f"Title: {entry['title']}\n\nBody:\n{entry['body']}"
    try:
        out = call_llm(
            EXTRACT_PROMPT.format(rfs=full),
            system=EXTRACT_SYSTEM, max_tokens=600, timeout=40,
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
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def emit(entry: dict, signals: dict) -> None:
    h = hashlib.sha1(entry["title"].encode()).hexdigest()[:14]
    item_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-yc-rfs-{h}"
    )
    item = {
        "id": item_id,
        "trace_id": new_trace_id(),
        "discovery_id": item_id,
        "stage": "validator",
        "source": "yc-rfs",
        "url": "https://www.ycombinator.com/rfs",
        "title": entry["title"],
        # YC RFS thesis IS the pain — verbose enough for downstream
        "pain_one_liner": signals.get("problem", "")[:240],
        "audience": signals.get("audience", ""),
        "monetization": signals.get("monetization", ""),
        "monetization_signal": signals.get("monetization_signal", "high"),
        "pricing_signal": signals.get("pricing_guess", ""),
        "tam_signal": signals.get("tam_signal", "high"),
        "axentx_idea": (signals.get("axentx_idea")
                        or signals.get("product_hypothesis", "")),
        "v1_wedge": signals.get("v1_wedge", ""),
        "incumbent_competitors": signals.get("incumbent_competitors", []),
        "yc_partner": signals.get("yc_partner", "unknown"),
        "why_now": signals.get("why_now", ""),
        "raw_signals": signals,
        # YC RFS = very high authority — flag for higher pitch-panel weight
        "authority_score": 0.95,
        "history": [{
            "stage": "research",
            "actor": "yc-rfs-tracker",
            "output": (f"YC RFS: {entry['title'][:80]} | "
                       f"audience={signals.get('audience','')[:80]} | "
                       f"v1={signals.get('v1_wedge','')[:120]}"),
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    write_item(item, "validator")


def do_one() -> bool:
    seen = load_seen()
    html = fetch_rfs_index()
    if not html:
        return False
    entries = parse_rfs_entries(html)
    log("yc-rfs", f"parsed {len(entries)} candidate RFS entries")

    new_count = 0
    emitted = 0
    for e in entries:
        if _stop:
            break
        h = hashlib.sha1(e["title"].encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        new_count += 1
        signals = extract_signals(e)
        if not signals:
            continue
        emit(e, signals)
        emitted += 1
        log("yc-rfs",
            f"  ✓ {e['title'][:60]} → validator "
            f"(audience={(signals.get('audience') or '')[:40]})")
        time.sleep(2)

    save_seen(seen)
    log("yc-rfs", f"cycle: {new_count} new RFS, emitted {emitted}")
    return new_count > 0


if __name__ == "__main__":
    daemon_loop("yc-rfs", POLL_SEC, do_one)
