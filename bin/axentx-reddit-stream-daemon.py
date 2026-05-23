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
    # # 2026-05-10 130-sub money-focused expansion
    # ── tech ops/dev (28) ──
    "devops,sre,sysadmin,aws,kubernetes,programming,saas,startups,"
    "selfhosted,homelab,terraform,azure,gcp,openshift,InfrastructureAsCode,"
    "devsecops,dataengineering,dataops,mlops,kubernetes,docker,linux,"
    "linuxadmin,networking,cybersecurity,netsec,websec,bash,"
    # ── eng-leader / IT-decision-maker (15) ──
    "EngineeringManagers,ExperiencedDevs,cscareerquestions,ITManagers,"
    "ITCareerQuestions,programminghorror,codereview,ChatGPTPro,LocalLLaMA,"
    "OpenAI,MachineLearning,datascience,learnmachinelearning,"
    "PromptEngineering,ArtificialIntelligence,"
    # ── founder / SaaS / startup (24) ──
    "Entrepreneur,EntrepreneurRideAlong,buildinpublic,microsaas,indiehackers,"
    "ycombinator,Founders,sweatystartup,startup_resources,Startup_Ideas,"
    "advancedentrepreneur,SaaS,B2BSaaS,SideProject,sidehustle,passive_income,"
    "kickstarter,IndieDev,fatFIRE,FinancialIndependence,SmallBusinessLoans,"
    "kickstarter,gofundme,Patreon,"
    # ── e-commerce / retail (12) ──
    "shopify,EtsySellers,dropship,Etsy,Amazon,FulfillmentByAmazon,"
    "AmazonSeller,AmazonFBA,bigcommerce,WooCommerce,ecommerce,"
    "FulfillmentByMerchant,"
    # ── product / design / UX (10) ──
    "ProductManagement,productmgmt,userexperience,UXDesign,UXResearch,"
    "graphic_design,web_design,FigmaDesign,Notion,ObsidianMD,"
    # ── sales / marketing / growth (16) ──
    "sales,salestechniques,marketing,digital_marketing,SEO,contentmarketing,"
    "EmailMarketing,growthhacking,advertising,copywriting,PPC,"
    "smallbusinessmarketing,b2bmarketing,Affiliatemarketing,DigitalMarketing,"
    "PaidSocial,"
    # ── finance / accounting / ops (12) ──
    "Accounting,bookkeeping,smallbusiness,Bookkeeping,taxhelp,tax,"
    "smallbusinessowner,QuickBooks,Xero,FinancialPlanning,FreshBooks,"
    "Bookkeepers,"
    # ── HR / recruiting / hiring (8) ──
    "AskHR,recruiting,recruitinghell,jobs,careeradvice,Workreform,"
    "hiring,humanresources,"
    # ── freelance / remote work (10) ──
    "freelance,Upwork,RemoteOK,RemoteJobs,remotework,WorkOnline,"
    "WFH,digitalnomad,RemoteWork,beermoney,"
    # ── frameworks / dev specific (16) ──
    "webdev,nextjs,reactjs,vuejs,nodejs,golang,rust,python,Python,"
    "learnpython,typescript,django,flask,laravel,Symfony,fastapi,"
    # ── healthcare / regulated (8) ──
    "healthcare,healthIT,nursing,medicine,HIPAA,pharma,MedicalDevice,"
    "biotech,"
    # ── legal / compliance (5) ──
    "law,legaladvice,paralegal,compliance,GDPR,"
    # ── data / analytics (8) ──
    "datascience,dataengineering,analytics,businessintelligence,"
    "PowerBI,tableau,Looker,Snowflake,"
    # ── automation / no-code (8) ──
    "NoCode,NoCodeSaaS,Zapier,Airtable,nocodeapps,Bubble,"
    "automation,IFTTT,"
    # ── customer/buyer pain (6) ──
    "BuyItForLife,reviews,assholedesign,customer_service,"
    "consumerbehaviour,techsupport,"
    # ── personal-finance / B2C money (6) ──
    "personalfinance,povertyfinance,MiddleClassFinance,Frugal,"
    "buildapc,EcoFriendly,"
    # ── verticals: gaming/content/creator (8) ──
    "gamedev,Unity3D,unrealengine,gamemaker,Twitch,"
    "smallStreamers,smallYTchannel,podcasting",
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

# 2026-05-09 supabase circuit breaker
# Supabase has been unreliable since 2026-05-07 (60+h outage). Some endpoints
# still respond (auth /, 401 ok), others time out (rpc/seen_*, table inserts).
# Without circuit breaker, each daemon wastes 15s × ~50 calls/cycle = 12.5min
# of wall-clock time per cycle on dead calls. Below: skip Supabase entirely
# after 5 consecutive failures, retry every 10 min.

import time as _time_cb
_sb_fail_count = 0
_sb_skip_until = 0


def _sb_should_skip() -> bool:
    """Return True if we should skip Supabase for now (circuit open)."""
    return _time_cb.time() < _sb_skip_until


def _sb_mark_fail() -> None:
    """Increment fail counter, open circuit if threshold reached."""
    global _sb_fail_count, _sb_skip_until
    _sb_fail_count += 1
    if _sb_fail_count >= 5:
        _sb_skip_until = _time_cb.time() + 600  # skip 10 min
        _sb_fail_count = 0


def _sb_mark_success() -> None:
    """Reset fail counter on success."""
    global _sb_fail_count
    _sb_fail_count = 0

def _sb(method: str, path: str, body=None, headers_extra=None):
    if not (SB_URL and SB_KEY) or _sb_should_skip():
        return None
    h = dict(SB_HEADERS)
    if headers_extra:
        h.update(headers_extra)
    data = json.dumps(body).encode() if body is not None else None
    if _sb_should_skip():
        return None
    req = urllib.request.Request(
        f"{SB_URL}/rest/v1/{path}", data=data, method=method, headers=h,
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            raw = r.read()
            _sb_mark_success()
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        _sb_mark_fail()
        log("reddit-stream",
            f"  sb {method} {path[:60]}: HTTP {e.code} {e.read()[:120]!r}")
        return None
    except Exception as e:
        _sb_mark_fail()
        log("reddit-stream",
            f"  sb {method} {path[:60]}: {type(e).__name__}: {str(e)[:120]}")
        return None

# 2026-05-08 CF-first dedup migration
# CF Worker /seen/check + /seen/mark — backed by D1, faster than Supabase.
# Use as primary; fall back to Supabase RPC only if CF fails/rate-limits.
import os as _os
_CF_DEDUP_URL = _os.environ.get("CF_DEDUP_URL") or _os.environ.get(
    "SHARED_DEDUP_URL", "https://surrogate-1-cursor.ashira.workers.dev"
).rstrip("/")


def _cf_seen_check(fps_list, kind="pain-url"):
    """POST /seen/check {kind, fps[]} -> {seen[], unseen[]}.
    Returns set of fps NOT yet seen (i.e. caller should process them).
    Returns None on failure so caller can fall back to Supabase."""
    if not fps_list:
        return set()
    chunk = fps_list[:200]
    body = json.dumps({"kind": kind, "fps": chunk}).encode()
    req = urllib.request.Request(
        f"{_CF_DEDUP_URL}/seen/check", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "axentx"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())
        return set(d.get("unseen") or [])
    except Exception:
        return None  # signal fallback


def _cf_seen_mark(fps_list, kind="pain-url", host=""):
    """POST /seen/mark {kind, fps[], host}. Returns True on success."""
    if not fps_list:
        return True
    chunk = fps_list[:200]
    body = json.dumps({"kind": kind, "fps": chunk, "host": host}).encode()
    req = urllib.request.Request(
        f"{_CF_DEDUP_URL}/seen/mark", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "axentx"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
        return True
    except Exception:
        return False


def already_seen(fp: str) -> bool:
    """Check if fp is in shared dedup. CF Worker /seen/check first (D1-backed),
    Supabase fallback. Returns True if fp has been seen before."""
    cf = _cf_seen_check([fp], kind="pain-url")
    if cf is not None:
        # CF returned successfully — fp is "seen" if NOT in unseen set
        return fp not in cf
    # CF failed — fall back to Supabase RPC
    r = _sb("POST", "rpc/seen_check_bulk", {
        "p_kind": "pain-url", "p_fps": [fp],
    })
    return isinstance(r, list) and len(r) > 0


def stamp_seen(fp: str, host: str = "reddit-stream") -> None:
    """Mark fp as seen in shared dedup. Write to CF (D1) first; also write to
    Supabase as best-effort backup so the legacy table stays consistent."""
    _cf_seen_mark([fp], kind="pain-url", host=host)
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
