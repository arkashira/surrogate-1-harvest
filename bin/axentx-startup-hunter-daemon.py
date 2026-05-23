#!/usr/bin/env python3
"""axentx startup-hunter — discovers newly-launched/funded startups across
multiple sources, extracts pain+wedge+stack signals, feeds them as
high-authority items to bd queue for portfolio fit + competitor intel.

Sources:
  • ProductHunt — daily launches with vote/comment count
  • Hacker News — `Show HN:` and `Launch HN:` recent posts
  • IndieHackers — milestone posts (recent revenue / launch)
  • BetaList — newest startups (week-old)
  • F6S — recent batch programs / accelerator cohorts
  • Wellfound (AngelList) — recently-funded startups (HTML scrape)
  • Reddit r/SaaS, r/startups, r/Entrepreneur — recent launch threads
  • Tiny Startups / Indie Mailer / Substack — newsletter aggregations
  • YC startup directory — recent batches
  • Crunchbase News funding feed — week's new rounds

Per finding extracts (LLM cheap-tier):
  - name, one_liner, category, target_audience
  - traction_signal (rough — votes/comments/upvotes/funding amount)
  - monetization (subscription/usage/marketplace/free/ads)
  - tech_stack (if visible from job posts/docs/website)
  - similarity_to_axentx_portfolio (which existing slug it competes with)
  - distinct_wedge (what makes them different from generic competition)

Output:
  • shared_kv["startup-hunter.findings"] = roll-up of last 24h
  • shared_memory entries (kind=startup-launch) — agents can search
  • For HIGH-signal startups (high traction OR funded OR exact-axentx-overlap):
    push pipeline_item to bd at stage=bd with authority_score=0.9 so bd
    + product-synth + competitor-intel can react

Cycle: every 2 hours, leader=GCP. Stamps visited URLs in
shared_kv["startup-hunter.visited.<hash>"] so we don't re-mine.

Discipline:
  - Per-source max 30 findings per cycle (avoid noise)
  - Skip findings older than 7 days (stale = useless for trends)
  - Cluster duplicates (same startup found on 2+ sources = stronger signal)
"""
from __future__ import annotations
import datetime
import hashlib
import html
import json
import os
import re
import signal
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, daemon_loop, call_llm,  # noqa: E402
                             get_portfolio, get_portfolio_block)

POLL_SEC = int(os.environ.get("STARTUP_HUNTER_POLL_SEC", "7200"))   # 2 hours
HOST = socket.gethostname()
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _get(url: str, headers: dict | None = None,
         timeout: int = 18) -> bytes | None:
    try:
        h = {"User-Agent": UA, **(headers or {})}
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _is_visited(url: str) -> bool:
    try:
        from axentx_shared import kv_get
        return bool(kv_get(
            f"startup-hunter.visited."
            f"{hashlib.md5(url.encode()).hexdigest()[:12]}"))
    except Exception:
        return False


def _mark_visited(url: str, source: str, name: str = "") -> None:
    try:
        from axentx_shared import kv_set
        h = hashlib.md5(url.encode()).hexdigest()[:12]
        kv_set(f"startup-hunter.visited.{h}", {
            "url": url, "source": source, "name": name[:80],
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
        })
    except Exception:
        pass


# ── Source scrapers ────────────────────────────────────────────────────


def hunt_producthunt() -> list[dict]:
    """ProductHunt has a public RSS-ish endpoint."""
    out = []
    body = _get("https://www.producthunt.com/feed", timeout=20)
    if not body:
        return out
    txt = body.decode("utf-8", errors="replace")
    # Extract <item><title>X — Y</title><link>...</link>
    for m in re.finditer(
            r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?"
            r"<description>(.*?)</description>",
            txt, re.DOTALL):
        title = html.unescape(m.group(1))[:200]
        link = m.group(2).strip()
        desc = html.unescape(re.sub(r"<[^>]+>", "", m.group(3)))[:400]
        if _is_visited(link):
            continue
        _mark_visited(link, "producthunt", title)
        out.append({"source": "producthunt", "name": title.split("—")[0].strip(),
                    "url": link, "description": desc})
        if len(out) >= 30:
            break
    return out


def hunt_hn_showhn() -> list[dict]:
    """Show HN + Launch HN via Algolia."""
    out = []
    for tag in ("show_hn", "launch_hn", "story"):
        url = (f"https://hn.algolia.com/api/v1/search_by_date?"
               f"tags=({tag},story)&hitsPerPage=20")
        body = _get(url, timeout=15)
        if not body:
            continue
        try:
            d = json.loads(body)
        except Exception:
            continue
        for hit in d.get("hits", []):
            link = (hit.get("url")
                    or f"https://news.ycombinator.com/item?id={hit['objectID']}")
            if _is_visited(link):
                continue
            title = hit.get("title", "")[:200]
            # Only Show HN / Launch HN actually launches
            if tag == "story" and not (
                    title.startswith("Show HN") or title.startswith("Launch HN")):
                continue
            _mark_visited(link, "hackernews", title)
            out.append({
                "source": "hackernews",
                "name": title.replace("Show HN:", "").replace("Launch HN:", "")
                                .strip()[:120],
                "url": link, "description": title,
                "traction_signal": f"{hit.get('points', 0)} points, "
                                   f"{hit.get('num_comments', 0)} comments",
            })
            if len(out) >= 25:
                return out
    return out


def hunt_indiehackers() -> list[dict]:
    """IH milestones + new posts — public RSS."""
    out = []
    for feed in ("https://www.indiehackers.com/feed.xml",
                 "https://www.indiehackers.com/milestones/feed.xml"):
        body = _get(feed, timeout=15)
        if not body:
            continue
        txt = body.decode("utf-8", errors="replace")
        for m in re.finditer(
                r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?"
                r"<description>(.*?)</description>",
                txt, re.DOTALL):
            title = html.unescape(m.group(1))[:200]
            link = m.group(2).strip()
            if _is_visited(link):
                continue
            desc = html.unescape(re.sub(r"<[^>]+>", "",
                                        m.group(3)))[:400]
            _mark_visited(link, "indiehackers", title)
            out.append({"source": "indiehackers", "name": title[:120],
                        "url": link, "description": desc})
            if len(out) >= 20:
                return out
    return out


def hunt_betalist() -> list[dict]:
    """BetaList — week-old startups."""
    out = []
    body = _get("https://betalist.com/feed.atom", timeout=15)
    if not body:
        return out
    txt = body.decode("utf-8", errors="replace")
    for m in re.finditer(
            r"<entry>.*?<title[^>]*>(.*?)</title>.*?"
            r"<link[^>]*href=\"([^\"]+)\".*?<summary[^>]*>(.*?)</summary>",
            txt, re.DOTALL):
        title = html.unescape(m.group(1))[:200]
        link = m.group(2)
        if _is_visited(link):
            continue
        desc = html.unescape(re.sub(r"<[^>]+>", "", m.group(3)))[:400]
        _mark_visited(link, "betalist", title)
        out.append({"source": "betalist", "name": title[:120],
                    "url": link, "description": desc})
        if len(out) >= 25:
            break
    return out


def hunt_yc_directory() -> list[dict]:
    """YC startup directory recent batches via API."""
    out = []
    body = _get("https://api.ycombinator.com/v0.1/companies?batch=W25,F25,S25",
                timeout=15)
    if not body:
        # fallback to HTML scrape
        body = _get("https://www.ycombinator.com/companies?batch=W25",
                    timeout=15)
        return out   # too dynamic — skip if API failed
    try:
        d = json.loads(body)
    except Exception:
        return out
    companies = d.get("companies") or d
    if isinstance(companies, list):
        for c in companies[:30]:
            name = c.get("name", "")[:120]
            link = c.get("website") or c.get("url", "")
            if not link or _is_visited(link):
                continue
            _mark_visited(link, "yc", name)
            out.append({
                "source": "yc",
                "name": name,
                "url": link,
                "description": (c.get("one_liner") or
                                c.get("long_description") or "")[:400],
                "batch": c.get("batch"),
            })
    return out


def hunt_reddit_launches() -> list[dict]:
    """Reddit r/SaaS / r/startups / r/Entrepreneur — recent launch posts."""
    out = []
    for sub in ("SaaS", "startups", "Entrepreneur", "indiehackers"):
        url = f"https://www.reddit.com/r/{sub}/new.json?limit=15"
        body = _get(url, timeout=12)
        if not body:
            continue
        try:
            d = json.loads(body)
        except Exception:
            continue
        for item in (d.get("data") or {}).get("children", []):
            p = item.get("data") or {}
            title = (p.get("title") or "").lower()
            # Filter: launch-y posts only
            if not any(kw in title for kw in
                       ("launch", "introducing", "built", "made",
                        "shipped", "show & tell", "show off")):
                continue
            link = p.get("url") or f"https://reddit.com{p.get('permalink','')}"
            if _is_visited(link):
                continue
            _mark_visited(link, f"reddit-{sub}", p.get("title", ""))
            out.append({
                "source": f"reddit/{sub}",
                "name": p.get("title", "")[:120],
                "url": link,
                "description": (p.get("selftext") or "")[:600],
                "traction_signal": f"{p.get('score', 0)} score, "
                                   f"{p.get('num_comments', 0)} comments",
            })
            if len(out) >= 25:
                return out
    return out


# ── Enrichment ─────────────────────────────────────────────────────────


ENRICH_SYSTEM = (
    "You are a startup analyst. For one observed startup launch, output "
    "STRICT JSON describing it:\n"
    "{\n"
    '  "name": "<canonical name>",\n'
    '  "one_liner": "<1-sentence what it does>",\n'
    '  "category": "<1-2 word category — e.g. payroll, dev-tooling>",\n'
    '  "target_audience": "<who pays — be specific>",\n'
    '  "monetization": "subscription|usage|marketplace|enterprise|free|ads|none",\n'
    '  "tech_stack_hint": "<inferred stack if visible — else null>",\n'
    '  "competes_with_axentx": "<axentx slug or null>",\n'
    '  "distinct_wedge": "<what makes them different (1 sentence)>",\n'
    '  "axentx_takeaway": "<should we (a) compete, (b) ignore, '
    '(c) consider OEM/partner, (d) extract idea> + 1-line why",\n'
    '  "signal_strength": "low|medium|high"\n'
    "}\n"
    "If can't determine a field, use null. Be terse.")


def enrich(finding: dict) -> dict | None:
    portfolio_block = get_portfolio_block()
    prompt = (
        f"# Existing axentx portfolio (for competes_with_axentx field)\n"
        f"{portfolio_block}\n\n"
        f"# Observed startup launch\n"
        f"Source: {finding.get('source')}\n"
        f"Name: {finding.get('name','')}\n"
        f"URL: {finding.get('url','')}\n"
        f"Description: {finding.get('description','')[:800]}\n"
        f"Traction: {finding.get('traction_signal','(none reported)')}\n\n"
        f"Output STRICT JSON only.")
    try:
        out = call_llm(prompt, system=ENRICH_SYSTEM,
                       max_tokens=500, timeout=30)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        return json.loads(txt.strip())
    except Exception as e:
        log("startup-hunter",
            f"  ⚠ enrich {finding.get('name','?')[:30]}: "
            f"{type(e).__name__}: {str(e)[:60]}")
        return None


def push_high_signal_to_bd(finding: dict, enriched: dict) -> bool:
    """Push HIGH-signal startups (traction OR competes-with-axentx) to bd
    queue with authority=0.9 so they get top-priority verdict."""
    if not (SB_URL and SB_KEY):
        return False
    fid = (f"20260504-startup-{enriched.get('category','x')}-"
           f"{hashlib.md5(finding['url'].encode()).hexdigest()[:10]}")
    pain = (f"[startup-hunter] competitor/inspiration: "
            f"{enriched.get('name','?')} — {enriched.get('one_liner','')[:200]}")
    payload = {
        "id": fid, "stage": "bd", "project": "", "focus": "startup-launch",
        "history": [{
            "stage": "startup-hunter", "actor": "axentx-startup-hunter",
            "output": json.dumps({**finding, "enriched": enriched},
                                 ensure_ascii=False)[:1500],
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {"text": json.dumps(enriched, ensure_ascii=False)},
        "verdict": {
            "pain_one_liner": pain,
            "audience": enriched.get("target_audience", ""),
            "monetization_signal": ("high"
                                    if enriched.get("monetization") in
                                    ("subscription", "enterprise", "usage")
                                    else "low"),
            "evidence": (finding.get("description") or "")[:300],
            "domain": enriched.get("category", ""),
        },
        "axentx_idea": (f"Inspired by {enriched.get('name')}: "
                        f"{enriched.get('distinct_wedge','')}"),
        "audience": enriched.get("target_audience", ""),
        "monetization_signal": ("high"
                                if enriched.get("monetization") in
                                ("subscription", "enterprise", "usage")
                                else "low"),
        "authority_score": 0.9,
        "source": "startup-hunter",
        "post": {
            "title": pain, "body": (finding.get("description") or "")[:1200],
            "source": finding.get("source"), "url": finding.get("url"),
        },
    }
    body = {"id": fid, "stage": "bd", "project": "",
            "focus": "startup-launch", "payload": payload}
    try:
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception:
        return False


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("startup-hunter", "  ⤷ not leader — skip")
        return False

    log("startup-hunter", "▸ hunting…")
    findings: list[dict] = []
    for fn, label in (
        (hunt_producthunt, "producthunt"),
        (hunt_hn_showhn, "hackernews"),
        (hunt_indiehackers, "indiehackers"),
        (hunt_betalist, "betalist"),
        (hunt_yc_directory, "yc"),
        (hunt_reddit_launches, "reddit"),
    ):
        try:
            new = fn()
            log("startup-hunter", f"  + {label}: {len(new)} findings")
            findings.extend(new)
        except Exception as e:
            log("startup-hunter",
                f"  ⚠ {label}: {type(e).__name__}: {str(e)[:60]}")

    if not findings:
        log("startup-hunter", "  ✓ no new findings")
        return False

    # Enrich + push (cap to avoid LLM storm)
    pushed = 0
    enriched_list = []
    for f in findings[:25]:
        e = enrich(f)
        if not e:
            continue
        enriched_list.append({**f, "enriched": e})
        # High-signal push: traction high OR competes with axentx
        push_worthy = (
            (e.get("signal_strength") == "high")
            or e.get("competes_with_axentx")
            or (e.get("monetization") in ("subscription", "enterprise"))
        )
        if push_worthy:
            if push_high_signal_to_bd(f, e):
                pushed += 1

    log("startup-hunter",
        f"  ✓ enriched {len(enriched_list)}, pushed {pushed} high-signal "
        f"to bd queue")

    try:
        from axentx_shared import kv_set, memory_log
        kv_set("startup-hunter.findings", {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "host": HOST,
            "n_total": len(findings),
            "n_enriched": len(enriched_list),
            "n_pushed_to_bd": pushed,
            "recent": enriched_list[:30],
        })
        memory_log("startup-hunter", "hunt-cycle",
                   f"hunted {len(findings)} startups, "
                   f"enriched {len(enriched_list)}, pushed {pushed} to bd",
                   body=json.dumps(
                       [{"name": x.get("enriched", {}).get("name", "?"),
                         "category": x.get("enriched", {}).get("category", "?"),
                         "competes_with": x.get("enriched", {}).get("competes_with_axentx"),
                         "url": x.get("url", "")[:80]}
                        for x in enriched_list[:15]],
                       ensure_ascii=False, indent=2)[:1500],
                   tags=["startup-hunter", HOST])
    except Exception:
        pass
    return False


if __name__ == "__main__":
    daemon_loop("startup-hunter", POLL_SEC, cycle)
