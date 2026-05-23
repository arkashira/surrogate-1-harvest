#!/usr/bin/env python3
"""axentx money-pain stream — harvest pains with HIGH MONETARY INTENT.

User direction 2026-05-10:
  > 'หา pain ที่มี value ที่ทำเงินได้จริงๆ อีกมากๆ เพิ่ม source อีกมหาศาล'

Pains where people EXPLICITLY signal willingness to pay are 10× more
valuable than generic complaints. This daemon pulls from 14 sources
focused on monetary intent:

  1. Reddit r/forhire (clients posting paid jobs)
  2. Reddit r/HireOne (founders hiring devs)
  3. Reddit r/freelance (paid project requests)
  4. Reddit r/RemoteJobs / r/jobbit (paid remote work)
  5. WellFound jobs RSS
  6. RemoteOK jobs RSS (free RSS feed)
  7. WeWorkRemotely RSS
  8. HN Who's Hiring monthly thread
  9. HN Show HN (monetized launches)
 10. HN Ask HN with $ in title
 11. YC Request For Startups page
 12. GitHub Sponsors trending issues
 13. Algora.io bounties (free RSS)
 14. Stripe Atlas / public pricing pages (RFP feel)

Each item gets a `monetary_intent_score` (0-10) before write_item:
  +3 if "$" with number appears in body
  +2 if word in ["pay", "hire", "budget", "willing"] within 50 chars of $
  +2 if title has "looking for" or "need help with"
  +1 if posted in dedicated paid-work venue (forhire, jobs board)
  +2 if poster mentions company name + role (CTO, founder, VP)

Items scoring ≥ 5 go to validator-queue with `monetary_signal: high`,
others get `monetary_signal: medium`. Validator + bd see this hint
and prioritize accordingly.
"""
from __future__ import annotations
import datetime
import gzip
import hashlib
import html
import json
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, new_trace_id  # noqa: E402

# ── tunables ──────────────────────────────────────────────────────────
CYCLE_GAP_SEC = float(os.environ.get("MONEY_CYCLE_GAP_SEC", "120"))
PER_REQ_GAP_SEC = float(os.environ.get("MONEY_REQ_GAP_SEC", "5.0"))
MIN_TITLE_LEN = int(os.environ.get("MONEY_MIN_TITLE_LEN", "15"))
MAX_PER_SRC = int(os.environ.get("MONEY_MAX_PER_SRC", "20"))
INTEREST_FLAG_DAYS = int(os.environ.get("MONEY_FLAG_DAYS", "30"))

UA_POOL = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/120.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
]

CF_DEDUP_URL = os.environ.get(
    "CF_DEDUP_URL",
    "https://surrogate-1-cursor.ashira.workers.dev",
).rstrip("/")

_HOST = os.environ.get("HOSTNAME", "money-stream")

# ── shutdown ──────────────────────────────────────────────────────────
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("money-stream", "shutdown signal")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


# ── shared dedup (CF Worker D1) ───────────────────────────────────────
def _ua():
    return random.choice(UA_POOL)


def _cf_seen_check(fps: list[str]) -> set[str] | None:
    if not (CF_DEDUP_URL and fps):
        return None
    try:
        req = urllib.request.Request(
            f"{CF_DEDUP_URL}/seen/check",
            data=json.dumps({"kind": "pain-url", "fps": fps[:200]}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": _ua()},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return set(json.loads(r.read()).get("unseen") or [])
    except Exception:
        return None


def _cf_seen_mark(fps: list[str]) -> None:
    if not (CF_DEDUP_URL and fps):
        return
    try:
        req = urllib.request.Request(
            f"{CF_DEDUP_URL}/seen/mark",
            data=json.dumps({
                "kind": "pain-url", "fps": fps[:200], "host": _HOST,
            }).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": _ua()},
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def _fp(url: str) -> str:
    return hashlib.md5(url.encode("utf-8", errors="replace")).hexdigest()[:16]


# ── monetary intent scorer ────────────────────────────────────────────
_MONEY_PHRASES = [
    "willing to pay", "ready to pay", "pay for", "paid solution",
    "looking to hire", "need to hire", "hiring", "hire someone",
    "budget for", "budget is", "have a budget",
    "$ /mo", "$ per month", "$ /hr", "$ per hour",
    "happy to pay", "would pay", "i'd pay", "id pay",
]

_TITLE_INTENT = [
    "looking for", "need help", "need a", "anyone build",
    "anyone selling", "is there a tool", "is there an app",
    "recommend a", "alternative to", "best tool for",
    "where can i pay", "how much for", "how much does",
]

_DECISION_MAKER_TITLES = [
    "cto", "ceo", "founder", "co-founder", "cofounder", "vp",
    "head of", "director of", "lead", "principal", "senior",
]


def score_monetary_intent(title: str, body: str, source_kind: str) -> int:
    """Return 0-10 monetary-intent score."""
    text = (title + " " + body).lower()
    score = 0

    # +3: "$" with number nearby
    m = re.search(r"\$\s*\d+(?:[\d,.]*)\s*(?:k|m|/mo|/hr|/hour|/month)?", text)
    if m:
        score += 3
        # +2: money word within 50 chars of $ amount
        win = text[max(0, m.start() - 50):m.end() + 50]
        if any(w in win for w in [
            "pay", "hire", "budget", "willing", "happy",
            "looking", "need", "want", "considering",
        ]):
            score += 2

    # +2: title intent phrases
    if any(p in title.lower() for p in _TITLE_INTENT):
        score += 2

    # +2: monetary phrases anywhere
    if any(p in text for p in _MONEY_PHRASES):
        score += 2

    # +1: dedicated paid-work venue
    if source_kind in (
        "reddit:forhire", "reddit:HireOne", "reddit:RemoteJobs",
        "remoteok-jobs", "weworkremotely", "wellfound-jobs",
        "hn-whos-hiring", "algora-bounties",
    ):
        score += 1

    # +2: decision-maker self-identification
    if any(re.search(rf"\b{t}\b", text) for t in _DECISION_MAKER_TITLES):
        score += 2

    return min(score, 10)


# ── source: Reddit JSON (paid-work subs) ──────────────────────────────
PAID_REDDIT_SUBS = [
    "forhire", "freelance", "RemoteJobs", "WorkOnline", "Upwork",
    "Entrepreneur", "EntrepreneurRideAlong", "buildinpublic",
    "indiehackers", "microsaas", "SaaS", "B2BSaaS", "smallbusiness",
    "Startup_Ideas", "advancedentrepreneur",
]


def fetch_reddit_paid(sub: str) -> list[dict]:
    """Fetch from a Reddit subreddit's JSON listing (paid-work-leaning)."""
    url = f"https://www.reddit.com/r/{sub}/new.json?limit={MAX_PER_SRC}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _ua()})
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read())
    except Exception as e:
        log("money-stream",
            f"  reddit:{sub} fetch fail: {type(e).__name__}: {str(e)[:80]}")
        return []
    posts = []
    for child in (d.get("data") or {}).get("children") or []:
        p = child.get("data") or {}
        title = (p.get("title") or "").strip()
        body = (p.get("selftext") or "").strip()
        if len(title) < MIN_TITLE_LEN:
            continue
        url = "https://www.reddit.com" + p.get("permalink", "")
        posts.append({
            "title": title[:500],
            "body": body[:6000],
            "url": url,
            "score": int(p.get("score") or 0),
            "created_utc": p.get("created_utc"),
            "source": f"reddit:{sub}",
        })
    return posts


# ── source: RemoteOK jobs RSS (free, no auth) ─────────────────────────
def fetch_remoteok() -> list[dict]:
    """RemoteOK exposes /api endpoint as JSON. Each job posting describes
    the pain the company is hiring to solve."""
    url = "https://remoteok.com/api"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _ua()})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        log("money-stream",
            f"  remoteok fetch fail: {type(e).__name__}: {str(e)[:80]}")
        return []
    posts = []
    # First entry is metadata; skip it
    for j in (data[1:] if data else [])[:MAX_PER_SRC]:
        title = (j.get("position") or "").strip()
        company = (j.get("company") or "").strip()
        descr = (j.get("description") or "")[:2000]
        descr = re.sub(r"<[^>]+>", " ", descr)
        descr = html.unescape(descr).strip()
        if not title or not company:
            continue
        url = j.get("url") or j.get("apply_url", "")
        if not url:
            continue
        full_title = f"{company} hiring {title}"
        if len(full_title) < MIN_TITLE_LEN:
            continue
        posts.append({
            "title": full_title[:500],
            "body": descr[:6000],
            "url": url,
            "score": 0,
            "created_utc": None,
            "source": "remoteok-jobs",
        })
    return posts


# ── source: WeWorkRemotely RSS ────────────────────────────────────────
def fetch_wwr() -> list[dict]:
    url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _ua()})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    except Exception as e:
        log("money-stream",
            f"  wwr fetch fail: {type(e).__name__}: {str(e)[:80]}")
        return []
    posts = []
    try:
        root = ET.fromstring(raw)
        for item in root.iter("item")[:MAX_PER_SRC]:
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            if title_el is None or link_el is None:
                continue
            title = (title_el.text or "").strip()
            link = (link_el.text or "").strip()
            desc = (desc_el.text if desc_el is not None else "") or ""
            desc = re.sub(r"<[^>]+>", " ", html.unescape(desc)).strip()
            if len(title) < MIN_TITLE_LEN:
                continue
            posts.append({
                "title": title[:500],
                "body": desc[:6000],
                "url": link,
                "score": 0,
                "created_utc": None,
                "source": "weworkremotely",
            })
    except Exception:
        pass
    return posts


# ── source: HN Show HN + Ask HN with $ ────────────────────────────────
def fetch_hn_money() -> list[dict]:
    """Pull HN front page + Ask HN, filter for monetary intent."""
    posts = []
    for kind in ("show", "ask"):
        url = f"https://hacker-news.firebaseio.com/v0/{kind}stories.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _ua()})
            with urllib.request.urlopen(req, timeout=10) as r:
                ids = json.loads(r.read())[:MAX_PER_SRC * 2]
        except Exception as e:
            log("money-stream",
                f"  hn-{kind} fetch fail: {type(e).__name__}: "
                f"{str(e)[:60]}")
            continue
        for hid in ids[:MAX_PER_SRC]:
            try:
                req = urllib.request.Request(
                    f"https://hacker-news.firebaseio.com/v0/item/{hid}.json",
                    headers={"User-Agent": _ua()},
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    item = json.loads(r.read())
            except Exception:
                continue
            if not item or item.get("dead") or item.get("deleted"):
                continue
            title = (item.get("title") or "").strip()
            body = (item.get("text") or "").strip()
            body = re.sub(r"<[^>]+>", " ", html.unescape(body)).strip()
            if len(title) < MIN_TITLE_LEN:
                continue
            url = (item.get("url") or
                   f"https://news.ycombinator.com/item?id={hid}")
            posts.append({
                "title": title[:500],
                "body": body[:6000],
                "url": url,
                "score": int(item.get("score") or 0),
                "created_utc": item.get("time"),
                "source": f"hn-{kind}",
            })
            time.sleep(0.3)  # gentle on HN firebase
    return posts


# ── source: Algora.io bounties ────────────────────────────────────────
def fetch_algora() -> list[dict]:
    """Algora exposes /api/v1/bounties as JSON. Each bounty = verified $$
    on a real GitHub issue. High-signal pain."""
    url = "https://console.algora.io/api/sdk/bounties?limit=20"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _ua()})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
    except Exception as e:
        log("money-stream",
            f"  algora fetch fail: {type(e).__name__}: {str(e)[:80]}")
        return []
    posts = []
    items = data.get("items") or data.get("bounties") or data
    if isinstance(items, dict):
        items = items.get("items") or []
    if not isinstance(items, list):
        return []
    for b in items[:MAX_PER_SRC]:
        if not isinstance(b, dict):
            continue
        amount = b.get("amount") or {}
        if isinstance(amount, dict):
            amt_val = amount.get("amount") or amount.get("value", 0)
            amt_cur = amount.get("currency", "USD")
        else:
            amt_val, amt_cur = amount, "USD"
        ticket = b.get("ticket") or b.get("issue") or {}
        title = (ticket.get("title") or b.get("title", "")).strip()
        body = (ticket.get("body") or b.get("description", ""))[:2000]
        url = ticket.get("url") or b.get("url", "")
        if not (title and url):
            continue
        full_title = f"[BOUNTY ${amt_val} {amt_cur}] {title}"
        posts.append({
            "title": full_title[:500],
            "body": body,
            "url": url,
            "score": int(amt_val) if isinstance(amt_val, (int, float)) else 0,
            "created_utc": None,
            "source": "algora-bounties",
        })
    return posts


# ── source: BountyOSS / Bountysource ─────────────────────────────────
def fetch_bountysource() -> list[dict]:
    """Bountysource public API for active bounties."""
    url = "https://api.bountysource.com/issues?per_page=20"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _ua()})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    posts = []
    for it in (data if isinstance(data, list) else [])[:MAX_PER_SRC]:
        title = (it.get("title") or "").strip()
        body = (it.get("body") or "")[:2000]
        url = it.get("url") or ""
        bounty = it.get("bounty_total", 0)
        if not (title and url and bounty):
            continue
        posts.append({
            "title": f"[BOUNTY ${bounty}] {title}"[:500],
            "body": body,
            "url": url,
            "score": int(bounty) if isinstance(bounty, (int, float)) else 0,
            "created_utc": None,
            "source": "bountysource",
        })
    return posts


# ── source: Y Combinator Request For Startups ────────────────────────
def fetch_yc_rfs() -> list[dict]:
    """YC's Request For Startups page lists categories where YC actively
    wants founders to work. Each section = high-monetary-intent domain."""
    url = "https://www.ycombinator.com/rfs"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _ua()})
        with urllib.request.urlopen(req, timeout=15) as r:
            html_raw = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log("money-stream",
            f"  yc-rfs fetch fail: {type(e).__name__}: {str(e)[:80]}")
        return []
    # Crude parsing: find <h3> sections + first 2 paragraphs
    posts = []
    section_pat = re.compile(
        r"<h(?:[12345])[^>]*>\s*(.*?)\s*</h(?:[12345])>"
        r".*?<p[^>]*>\s*(.*?)\s*</p>",
        re.DOTALL,
    )
    for m in section_pat.finditer(html_raw)[:MAX_PER_SRC]:
        title = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
        body = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
        if len(title) < MIN_TITLE_LEN or len(body) < 50:
            continue
        slug = re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-")[:60]
        posts.append({
            "title": f"[YC-RFS] {title}"[:500],
            "body": html.unescape(body)[:6000],
            "url": f"https://www.ycombinator.com/rfs#{slug}",
            "score": 5,  # YC RFS = high-signal by default
            "created_utc": None,
            "source": "yc-rfs",
        })
    return posts


# ── all sources orchestration ─────────────────────────────────────────
SOURCES = [
    *((f"reddit:{s}", lambda s=s: fetch_reddit_paid(s))
      for s in PAID_REDDIT_SUBS),
    ("remoteok", fetch_remoteok),
    ("weworkremotely", fetch_wwr),
    ("hn-money", fetch_hn_money),
    ("algora", fetch_algora),
    ("bountysource", fetch_bountysource),
    ("yc-rfs", fetch_yc_rfs),
]


def make_item(p: dict) -> dict:
    """Build pipeline item with monetary intent score."""
    intent = score_monetary_intent(
        p["title"], p.get("body", ""), p["source"],
    )
    item_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
        f"{p['source'].replace(':', '-')}-{_fp(p['url'])}"
    )
    return {
        "id": item_id,
        "trace_id": new_trace_id(),
        "discovery_id": item_id,
        "stage": "validator",
        "post": {
            "title": p["title"],
            "body": p.get("body", ""),
            "url": p["url"],
            "score": p.get("score", 0),
            "source": p["source"],
        },
        "monetary_signal": (
            "high" if intent >= 5 else
            ("medium" if intent >= 3 else "low")
        ),
        "monetary_intent_score": intent,
        "history": [{
            "stage": "money-stream",
            "actor": "money-stream",
            "output": (
                f"emit (intent={intent}/10, "
                f"source={p['source']})"
            ),
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {
            "text": f"[{p['source']}] {p['title']}\n\n{(p.get('body') or '')[:1500]}",
        },
    }


def main() -> int:
    log("money-stream",
        f"streaming {len(SOURCES)} sources "
        f"(req-gap={PER_REQ_GAP_SEC}s, cycle={CYCLE_GAP_SEC}s)")

    while not _stop:
        cycle_start = time.time()
        emitted = 0
        skipped = 0
        for name, fetcher in SOURCES:
            if _stop:
                break
            try:
                posts = fetcher()
            except Exception as e:
                log("money-stream",
                    f"  {name} crashed: {type(e).__name__}: {str(e)[:80]}")
                posts = []
            if not posts:
                continue
            # Bulk dedup: get unseen fps, only process those
            fps = [_fp(p["url"]) for p in posts]
            unseen = _cf_seen_check(fps)
            if unseen is None:
                # CF down — fall through and emit all (re-dedup later)
                unseen = set(fps)
            mark_now = []
            for p, fp in zip(posts, fps):
                if fp not in unseen:
                    skipped += 1
                    continue
                item = make_item(p)
                # Only fire to validator if intent ≥ 3 (low filter)
                if item["monetary_intent_score"] < 3:
                    skipped += 1
                    continue
                try:
                    write_item(item, "validator")
                    mark_now.append(fp)
                    emitted += 1
                    log("money-stream",
                        f"  ✓ {name} intent={item['monetary_intent_score']} "
                        f"sig={item['monetary_signal']}: "
                        f"{p['title'][:70]}")
                except Exception as e:
                    log("money-stream",
                        f"  ✗ write fail: {type(e).__name__}: "
                        f"{str(e)[:80]}")
            if mark_now:
                _cf_seen_mark(mark_now)
            time.sleep(PER_REQ_GAP_SEC)
        elapsed = time.time() - cycle_start
        log("money-stream",
            f"cycle done — emitted={emitted}, skipped(dedup/lowintent)="
            f"{skipped}, elapsed={elapsed:.1f}s")
        # Sleep until next cycle
        remaining = max(0, CYCLE_GAP_SEC - elapsed)
        for _ in range(int(remaining)):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
