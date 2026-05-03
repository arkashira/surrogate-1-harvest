#!/usr/bin/env python3
"""axentx Reddit stream — continuous pain-signal harvester.

Streams (NOT crons) Reddit subreddits via the public JSON API. Each
post that matches pain heuristics (frustration markers, "how do I",
"why does X fail") becomes a research-queue item flowing through the
existing chain (research → validator → bd → spawn → ...).

Anti-bot strategy (no Reddit account needed):
  - Public *.json endpoints — no OAuth, no login wall
  - Realistic browser User-Agent per cycle
  - Respectful 6s gap per request (~10 req/min, Reddit's documented
    soft limit for unauthenticated clients)
  - Round-robin across 8 subreddits = each refreshed ~every 60s
  - Per-URL fingerprint stamped into Supabase seen_stamps for dedup
  - Posts older than INTEREST_FLAG_DAYS get flagged for periodic
    recheck rather than dropped (social-listening pattern)

Output:
  - new pain items → research-queue (Supabase pipeline_items)
  - dedup stamps → seen_stamps
  - interesting-but-pending-recheck → flagged_stamps (added to schema)
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, write_item, new_trace_id,  # noqa: E402
                             new_item)

# ── tunables ──────────────────────────────────────────────────────────────
SUBS = os.environ.get(
    "REDDIT_SUBS",
    "devops,sre,sysadmin,aws,kubernetes,programming,saas,startups,"
    "selfhosted,homelab,personalfinance,buildapc,smallbusiness",
).split(",")
LISTING = os.environ.get("REDDIT_LISTING", "new")  # new|hot|top|rising
PER_REQ_GAP_SEC = float(os.environ.get("REDDIT_REQ_GAP_SEC", "6.5"))
CYCLE_GAP_SEC = float(os.environ.get("REDDIT_CYCLE_GAP_SEC", "30"))
MAX_POSTS_PER_SUB = int(os.environ.get("REDDIT_MAX_POSTS", "25"))
MIN_TITLE_LEN = int(os.environ.get("REDDIT_MIN_TITLE_LEN", "20"))
INTEREST_FLAG_DAYS = int(os.environ.get("REDDIT_FLAG_DAYS", "30"))

UA_POOL = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/120.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
]

# ── Supabase coord plane ──────────────────────────────────────────────────
SB_URL = os.environ.get(
    "SUPABASE_URL", "https://riunimyxoalicbntogbp.supabase.co",
).rstrip("/")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
SB_HEADERS = {
    "apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
    "Content-Type": "application/json",
}

# Pain heuristics — phrases that indicate someone is venting/asking
PAIN_PATTERNS = [
    r"\bwhy\s+(?:does|is|the\s+hell)\b",
    r"\b(?:cannot|can't|unable\s+to|won't|fails|broken|stuck)\b",
    r"\b(?:hate|sucks|frustrat|painful|nightmare|disaster)\b",
    r"\b(?:looking\s+for\s+(?:a|an|the)|recommend|alternative\s+to)\b",
    r"\bhow\s+(?:do|to|can\s+I)\b.*\?",
    r"\b(?:bug|error|issue|problem)\s+(?:with|in|when)\b",
    r"\bis\s+there\s+(?:a|any)\s+(?:way|tool|solution)\b",
    r"\bmissing\s+(?:from|in|out\s+of)\b.*\bworkflow\b",
]
PAIN_RE = re.compile("|".join(PAIN_PATTERNS), re.IGNORECASE)


_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("reddit-stream", "shutdown signal")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


# ── Supabase helpers ──────────────────────────────────────────────────────
def _sb(method: str, path: str, body=None, headers_extra=None):
    if not (SB_URL and SB_KEY):
        return None
    h = dict(SB_HEADERS)
    if headers_extra:
        h.update(headers_extra)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{SB_URL}/rest/v1/{path}", data=data, method=method, headers=h,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        log("reddit-stream",
            f"  sb {method} {path[:60]}: HTTP {e.code} {e.read()[:120]!r}")
        return None
    except Exception as e:
        log("reddit-stream",
            f"  sb {method} {path[:60]}: {type(e).__name__}: {str(e)[:120]}")
        return None


def already_seen(fp: str) -> bool:
    """seen_check_bulk RPC returns rows for fps that ARE seen (contract:
    `[{"fp": "..."}]` for found, `[]` for not found). The earlier wrong
    check `r[0].get("seen", False)` always returned False because the
    RPC never emits a 'seen' field — it just returns the matching rows.
    This was the root cause of GitHub stream re-emitting same titles 3+
    times in 15min."""
    r = _sb("POST", "rpc/seen_check_bulk", {
        "p_kind": "pain-url", "p_fps": [fp],
    })
    return isinstance(r, list) and len(r) > 0


def stamp_seen(fp: str, host: str = "reddit-stream") -> None:
    """Use existing RPC seen_mark_bulk."""
    _sb("POST", "rpc/seen_mark_bulk", {
        "p_kind": "pain-url", "p_fps": [fp], "p_host": host,
    })


def stamp_flagged(fp: str, url: str, score: int, reason: str) -> None:
    """flagged_stamps table is optional — table may not exist yet.
    Failures are silently ignored so the daemon keeps streaming."""
    _sb("POST", "flagged_stamps", {
        "fp": fp, "url": url, "score": score, "reason": reason,
        "source": "reddit",
        "flagged_at": datetime.datetime.utcnow().isoformat() + "Z",
        "recheck_after": (datetime.datetime.utcnow()
                          + datetime.timedelta(days=1)).isoformat() + "Z",
    }, {"Prefer": "return=minimal,resolution=ignore-duplicates"})


# ── Reddit ────────────────────────────────────────────────────────────────
# Reddit blocks data-center IPs (GCP/Kamatera) on www.reddit.com/*.json
# (verified 2026-05-03: HTTP 403 across all 13 subs). Workarounds tried in
# order:
#   1. old.reddit.com — sometimes lighter blocking
#   2. .rss endpoint — Atom XML, less filtered
#   3. teddit.net public mirror — fully unblocked
import xml.etree.ElementTree as ET

REDDIT_HOSTS = [
    "https://old.reddit.com",
    "https://www.reddit.com",
]


def parse_rss(text: str) -> list[dict]:
    """Reddit RSS → list of post dicts compatible with .json shape."""
    posts = []
    try:
        root = ET.fromstring(text)
    except Exception:
        return posts
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns):
        title_el = entry.find("a:title", ns)
        link_el = entry.find("a:link", ns)
        content_el = entry.find("a:content", ns)
        published_el = entry.find("a:published", ns)
        permalink = (link_el.get("href") if link_el is not None else "")
        content_html = (content_el.text or "" if content_el is not None
                        else "")
        # crude HTML strip
        body = re.sub(r"<[^>]+>", " ", content_html)
        body = re.sub(r"&[a-z#0-9]+;", " ", body)
        # Approximate created_utc
        created = 0
        if published_el is not None and published_el.text:
            try:
                created = int(datetime.datetime.fromisoformat(
                    published_el.text.replace("Z", "+00:00")
                ).timestamp())
            except Exception:
                pass
        posts.append({"data": {
            "title": (title_el.text if title_el is not None else "").strip(),
            "selftext": body.strip()[:4000],
            "permalink": (permalink.replace("https://www.reddit.com", "")
                          .replace("https://old.reddit.com", "")),
            "url": permalink,
            "score": 0,                # RSS doesn't include score
            "created_utc": created,
        }})
    return posts


def fetch_sub(sub: str) -> list[dict]:
    """Try .json first (richer data), fall back to .rss when blocked."""
    for host in REDDIT_HOSTS:
        for ext in (".json", ".rss"):
            url = (f"{host}/r/{sub}/{LISTING}{ext}"
                   f"?limit={MAX_POSTS_PER_SUB}")
            req = urllib.request.Request(url, headers={
                "User-Agent": random.choice(UA_POOL),
                "Accept": ("application/json" if ext == ".json"
                           else "application/rss+xml, application/atom+xml"),
                "Accept-Language": "en-US,en;q=0.9",
            })
            try:
                with urllib.request.urlopen(req, timeout=12) as r:
                    raw = r.read()
                if ext == ".json":
                    d = json.loads(raw)
                    return d.get("data", {}).get("children", [])
                else:
                    return parse_rss(raw.decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(20)
                # try next host/ext
                continue
            except Exception:
                continue
    return []


def is_pain_signal(title: str, body: str) -> tuple[bool, str]:
    """Return (matched, signal_label)."""
    full = f"{title}\n{body[:1500]}"
    if len(title) < MIN_TITLE_LEN:
        return False, ""
    m = PAIN_RE.search(full)
    if m:
        return True, m.group(0)[:60]
    return False, ""


def post_to_pipeline(post: dict, sub: str) -> bool:
    """Convert Reddit post → pipeline_items row in research stage."""
    title = post.get("title", "").strip()
    body = (post.get("selftext") or "").strip()
    permalink = post.get("permalink", "")
    url = f"https://reddit.com{permalink}" if permalink else post.get("url", "")
    score = int(post.get("score", 0) or 0)
    age_days = (time.time() - int(post.get("created_utc", 0))) / 86400

    fp = hashlib.sha1(url.encode()).hexdigest()[:16]
    if already_seen(fp):
        return False

    matched, signal = is_pain_signal(title, body)
    if not matched:
        # Not a clear pain — but if score>=20 and recent, flag for recheck
        if score >= 20 and age_days <= INTEREST_FLAG_DAYS:
            stamp_flagged(fp, url, score, "high-score-but-no-pain-marker")
        stamp_seen(fp)  # don't reprocess
        return False

    # Build pipeline item — reuse existing axentx_pipeline.new_item shape
    discovery_id = new_trace_id()
    ts_iso = datetime.datetime.utcnow().isoformat() + "Z"
    item_id = (f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
               f"-reddit-{fp}")
    item = {
        "id": item_id,
        "discovery_id": discovery_id,
        "trace_id": discovery_id,
        "stage": "research",
        "created_at": ts_iso,
        "post": {
            "title": title, "body": body[:4000], "url": url,
            "score": score, "subreddit": sub, "age_days": round(age_days, 1),
            "signal": signal,
        },
        "history": [{
            "stage": "research", "actor": "axentx-reddit-stream",
            "output": json.dumps({
                "title": title[:200], "url": url, "signal": signal,
                "score": score, "sub": sub,
            }, ensure_ascii=False),
            "at": ts_iso,
        }],
        "current": {"text": f"[reddit/{sub}] {title}\n\n{body[:1500]}"},
    }
    # Write directly to validator-queue (skip research stage). Stream
    # daemons already heuristic-matched pain signal via PAIN_RE so the
    # validator's job (cross-source confirm + LLM verdict) starts
    # immediately. Earlier wrote to 'research' but no daemon consumes
    # research-queue (it's an SOURCE stage, not a CONSUMER stage), so
    # 2747 items piled up unprocessed. Verified 2026-05-03.
    item["stage"] = "validator"
    write_item(item, "validator")
    stamp_seen(fp)
    log("reddit-stream",
        f"  ✓ pain (score={score} age={age_days:.1f}d sig={signal!r}): "
        f"{title[:70]}")
    return True


def main() -> int:
    if not SB_KEY:
        log("reddit-stream", "FATAL: SUPABASE_SECRET_KEY not set")
        return 1
    log("reddit-stream",
        f"streaming {len(SUBS)} subs (gap={PER_REQ_GAP_SEC}s/req, "
        f"cycle={CYCLE_GAP_SEC}s)")

    while not _stop:
        cycle_start = time.time()
        emitted = 0
        for sub in SUBS:
            if _stop:
                break
            posts = fetch_sub(sub)
            for child in posts:
                p = child.get("data") or {}
                if post_to_pipeline(p, sub):
                    emitted += 1
            time.sleep(PER_REQ_GAP_SEC)
        elapsed = time.time() - cycle_start
        log("reddit-stream",
            f"cycle done — emitted {emitted} new pains in {elapsed:.0f}s")
        # Sleep to fill out cycle; if cycle was already long, just go again
        nap = max(0, CYCLE_GAP_SEC - elapsed)
        for _ in range(int(nap)):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
