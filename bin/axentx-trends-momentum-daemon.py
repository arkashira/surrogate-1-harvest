#!/usr/bin/env python3
"""axentx trends-momentum — pulls Google Trends + ExplodingTopics-style
momentum signals via free public endpoints. Tags pain items currently in
the validator/bd queue with a `momentum_score` so emerging niches get
priority.

Sources (no auth needed):
  1. Google Trends Daily Trending: https://trends.google.com/trending/rss?geo=US
  2. https://www.exploding-topics.com/topics (HTML scrape — top 100/wk)
  3. /r/popular cross-listed pain queries (volume hint)
  4. GitHub /trending HTML (technical momentum)

Output: enriches existing pipeline_items by setting `momentum_score`
0.0-1.0 in shared_kv["momentum.<term>"] — bd-daemon reads it to boost
verdict GO probability for trending pains.
"""
from __future__ import annotations
import datetime
import json
import os
import re
import signal
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("TRENDS_POLL_SEC", "10800"))   # 3h
UA = "Mozilla/5.0 (compatible; axentx-discovery/1.0)"

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))
signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("_stop", True))


def _http_get(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def fetch_google_trends() -> list[str]:
    """Google Trends RSS — daily trending searches per country."""
    out = []
    for geo in ("US", "GB", "TH", ""):
        xml = _http_get(
            f"https://trends.google.com/trending/rss?geo={geo}")
        for m in re.finditer(
                r"<title>\s*<!\[CDATA\[(.*?)\]\]>", xml, re.DOTALL)[:30]:
            t = m.group(1).strip()
            if t and len(t) > 3 and t.lower() != "daily search trends":
                out.append(t.lower())
    return out


def fetch_exploding_topics() -> list[str]:
    """exploding-topics.com — top emerging topics page."""
    html = _http_get("https://www.exploding-topics.com/blog/exploding-topics")
    out = []
    for m in re.finditer(r'<a[^>]+>([A-Za-z][\w\s\-\.]{4,40})</a>', html)[:80]:
        t = m.group(1).strip().lower()
        if t and not any(b in t for b in ("read more", "the", "blog")):
            out.append(t)
    return out


def fetch_github_trending() -> list[str]:
    """GitHub /trending — repo names + descriptions = tech momentum."""
    html = _http_get("https://github.com/trending")
    out = []
    for m in re.finditer(r'<a[^>]+href="/[^"]+/[^"]+"[^>]*class="Link"[^>]*>'
                         r'\s*([\w\-]+)\s*</a>', html)[:30]:
        out.append(m.group(1).lower())
    return out


def do_one():
    if _stop:
        return False
    try:
        from axentx_shared import kv_set
    except Exception:
        log("trends-momentum", "  ⚠ axentx_shared unavailable; skip")
        return False

    gt = fetch_google_trends()
    et = fetch_exploding_topics()
    ght = fetch_github_trending()
    all_terms = list({t for t in (gt + et + ght) if 3 < len(t) < 50})

    # Score: how many sources mentioned each term?
    score_map = {}
    for t in all_terms:
        s = 0
        if t in gt:
            s += 0.4
        if t in et:
            s += 0.4
        if t in ght:
            s += 0.2
        score_map[t] = round(min(s, 1.0), 2)

    # Save the whole map for bd-daemon to query at runtime
    payload = {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "terms": score_map,
        "n_terms": len(score_map),
    }
    kv_set("momentum.snapshot", payload)
    log("trends-momentum",
        f"cycle: {len(gt)} google + {len(et)} exploding + "
        f"{len(ght)} github = {len(score_map)} unique terms snapshot")
    return True


if __name__ == "__main__":
    daemon_loop("trends-momentum", POLL_SEC, do_one)
