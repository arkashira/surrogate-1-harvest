#!/usr/bin/env python3
"""axentx hackernews-pain-stream — pulls HN Show HN + Ask HN + comments
that signal real product pain or "I'd pay for X" requests.

Different from existing scrape-hn-discussions.sh (training data) — this
emits to validator-queue for the product-spawner pipeline.

Sources:
  1. https://hn.algolia.com/api/v1/search?tags=show_hn — launches
  2. https://hn.algolia.com/api/v1/search?tags=ask_hn  — questions
  3. https://hn.algolia.com/api/v1/search?query=...    — pain keywords

Looks for posts with:
  - "I'd pay for", "anyone need", "what do you use for"
  - Show HN with "$XX/mo" pricing in title or body
  - Ask HN about a problem people pay to solve
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

POLL_SEC = int(os.environ.get("HN_PAIN_POLL_SEC", "900"))   # 15 min
SEEN_FILE = REPO_ROOT / "state" / "hn-pain-stream.seen.json"
SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)

PAIN_QUERIES = [
    "i'd pay for",
    "anyone need",
    "what do you use for",
    "tired of",
    "wish there was",
    "would you pay",
    "looking for tool",
    "alternative to",
    "$$$ MRR",
    "indie hackers",
]

EXTRACT_SYSTEM = (
    "You are a startup-pain analyst reading HackerNews posts/comments. "
    "Decide if this signals a REAL pain that people would PAY money to fix "
    "(B2B SaaS / paid tool — not free open-source itch). Be skeptical: "
    "rejection of low-quality signals is fine."
)

EXTRACT_PROMPT = """HN post (title + body, max 4000 chars):

{post}

Output STRICT JSON:
{{
  "is_pain": true|false,
  "pain": "1-sentence pain (if is_pain=true), else empty",
  "audience": "who has this pain — be specific (e.g., 'data engineers at Series-A startups')",
  "monetization_signal": "low|medium|high — would they PAY money?",
  "evidence_quote": "1 quote from the post that proves the pain (max 200 chars)",
  "axentx_idea": "if is_pain=true: 1-sentence product idea; else null",
  "skip_reason": "if not is_pain: why not (1 sentence); else null"
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
        SEEN_FILE.write_text(json.dumps(sorted(seen)[-5000:]))
    except Exception:
        pass


def fetch_hn(tags: str = "", query: str = "", hits: int = 30) -> list[dict]:
    """Algolia HN search — public, no auth. Returns hits."""
    params = {"hitsPerPage": str(hits)}
    if tags:
        params["tags"] = tags
    if query:
        params["query"] = query
    url = f"https://hn.algolia.com/api/v1/search?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
        return data.get("hits", [])
    except Exception as e:
        log("hn-pain", f"  ✗ {tags or query}: {type(e).__name__}: {str(e)[:80]}")
        return []


def extract_signals(post: dict) -> dict | None:
    title = post.get("title") or post.get("story_title") or ""
    body = post.get("story_text") or post.get("comment_text") or ""
    body = re.sub(r"<[^>]+>", " ", body)
    full = f"Title: {title}\n\nBody:\n{body[:4000]}"
    try:
        out = call_llm(
            EXTRACT_PROMPT.format(post=full),
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


def emit(post: dict, signals: dict) -> None:
    obj_id = post.get("objectID") or hashlib.sha1(
        (post.get("url") or post.get("title") or "").encode()).hexdigest()[:16]
    item_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
        f"hn-{obj_id[:14]}"
    )
    item = {
        "id": item_id,
        "trace_id": new_trace_id(),
        "discovery_id": item_id,
        "stage": "validator",
        "source": "hackernews",
        "url": (post.get("url")
                or f"https://news.ycombinator.com/item?id={obj_id}"),
        "title": post.get("title") or post.get("story_title", ""),
        "pain_one_liner": signals.get("pain", "")[:240],
        "audience": signals.get("audience", ""),
        "monetization_signal": signals.get("monetization_signal", "low"),
        "evidence": signals.get("evidence_quote", ""),
        "axentx_idea": signals.get("axentx_idea") or "",
        "raw_signals": signals,
        "history": [{
            "stage": "research",
            "actor": "hn-pain-stream",
            "output": (f"hn: {(post.get('title') or '')[:80]} | "
                       f"pain={signals.get('pain','')[:140]} | "
                       f"mon={signals.get('monetization_signal','?')}"),
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    write_item(item, "validator")


def do_one() -> bool:
    seen = load_seen()
    new_count = 0
    emitted = 0
    sources = [("show_hn", ""), ("ask_hn", "")]
    sources += [("", q) for q in PAIN_QUERIES[:5]]
    for tags, query in sources:
        if _stop:
            break
        posts = fetch_hn(tags=tags, query=query, hits=20)
        for p in posts:
            obj_id = p.get("objectID")
            if not obj_id or obj_id in seen:
                continue
            seen.add(obj_id)
            new_count += 1
            signals = extract_signals(p)
            if not signals or not signals.get("is_pain"):
                continue
            mon = (signals.get("monetization_signal") or "").lower()
            if mon not in ("medium", "high"):
                continue
            emit(p, signals)
            emitted += 1
            log("hn-pain",
                f"  ✓ {(p.get('title') or '')[:60]} → validator "
                f"(mon={mon})")
        time.sleep(1)
    save_seen(seen)
    log("hn-pain", f"cycle: {new_count} new, emitted {emitted}")
    return new_count > 0


if __name__ == "__main__":
    daemon_loop("hn-pain-stream", POLL_SEC, do_one)
