#!/usr/bin/env python3
"""axentx competitor-intel — for each spawned product, do BuiltWith-style
stack analysis + traffic estimate against named competitors. Output goes
to /business/competitor-intel.md in the project repo.

Triggers on items in stage='design' OR 'pitch' (right after BMC). Reads
bd_verdict.incumbent_competitors[] OR pitch_result for competitor names,
then for each:
  1. fetch their landing page → extract pricing tier + headline
  2. WhatRuns/BuiltWith-equivalent: extract tech stack hints from HTML
  3. Try Wayback Machine for traffic-trend hint (snapshot count proxy)
  4. LLM consolidate → competitive table

This makes pitch decisions concrete: "buyer pays $X for incumbent Y at
moat Z; our angle is W."
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
from axentx_pipeline import (log, call_llm, write_item, daemon_loop,  # noqa: E402
                             pick_oldest, advance, get_role_budget)

POLL_SEC = int(os.environ.get("COMPETITOR_INTEL_POLL_SEC", "60"))
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
INTEL_BUDGET = get_role_budget("competitor-intel", 1500)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


INTEL_SYSTEM = (
    "You are a competitive analyst writing a 1-pager on each competitor. "
    "Given the competitor's homepage HTML/text, extract pricing, audience, "
    "tech stack hints, and our differentiation angle. Be specific — actual "
    "$ numbers, real tech names, no abstract fluff."
)

INTEL_PROMPT = """Our product hypothesis:
{hypothesis}

Our audience (per BD): {audience}
Our pricing tier (per BD): {pricing}

Competitor: {competitor}
Their homepage (first 6000 chars):
{homepage_text}

Output STRICT JSON:
{{
  "competitor_name": "{competitor}",
  "their_headline": "their value-prop headline (1 sentence)",
  "their_audience": "who they sell to",
  "their_pricing_tier": "their cheapest tier $/mo if visible, else 'unknown'",
  "their_tech_stack_hints": ["framework or platform clues from HTML — e.g., 'Webflow', 'Next.js', 'Stripe'"],
  "their_moat": "what's their defensibility (1 sentence)",
  "our_differentiation": "1-sentence angle WE could win — be specific",
  "should_we_compete": true|false,
  "rationale": "1-sentence why or why not"
}}
"""


def _http_get(url, timeout=15):
    if not url.startswith("http"):
        url = "https://" + url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"(fetch failed: {type(e).__name__})"


def html_to_text(html):
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


def find_competitor_url(name):
    """Best-effort: try a few obvious URL patterns."""
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", name).lower()
    candidates = [
        f"https://{cleaned}.com",
        f"https://{cleaned}.io",
        f"https://www.{cleaned}.com",
    ]
    for url in candidates:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA}, method="HEAD")
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status < 400:
                    return url
        except Exception:
            continue
    return None


def analyze_competitor(competitor: str, hyp: str, audience: str,
                      pricing: str) -> dict | None:
    url = find_competitor_url(competitor)
    if not url:
        return {"competitor_name": competitor, "error": "url not found"}
    homepage = html_to_text(_http_get(url))[:6000]
    if not homepage or homepage.startswith("(fetch failed"):
        return {"competitor_name": competitor, "url": url,
                "error": "homepage fetch failed"}
    try:
        out = call_llm(
            INTEL_PROMPT.format(
                competitor=competitor, hypothesis=hyp,
                audience=audience, pricing=pricing,
                homepage_text=homepage,
            ),
            system=INTEL_SYSTEM, max_tokens=INTEL_BUDGET, timeout=40,
        )
    except Exception as e:
        return {"competitor_name": competitor, "url": url,
                "error": f"LLM: {e}"}
    txt = out.strip()
    if "```" in txt:
        seg = txt.split("```", 2)
        if len(seg) >= 2:
            txt = seg[1]
            if txt.startswith("json"):
                txt = txt[4:]
            txt = txt.strip()
    try:
        d = json.loads(txt)
    except Exception:
        return {"competitor_name": competitor, "url": url,
                "error": "parse fail", "raw": txt[:300]}
    d["url"] = url
    return d


def render_intel_md(item: dict, analyses: list[dict]) -> str:
    bd = item.get("bd_verdict") or {}
    out = [
        f"# Competitor intel: {item.get('project','?')}",
        f"- generated: {datetime.datetime.utcnow().isoformat()}Z",
        f"- our hypothesis: {bd.get('new_product_one_liner') or bd.get('feature_one_liner','?')}",
        f"- our audience: {bd.get('buyer_persona','?')}",
        f"- our pricing tier: {bd.get('pricing_tier','?')}",
        "",
        "## Competitive matrix",
        "",
        "| competitor | url | their headline | their pricing | moat | our angle | compete? |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in analyses:
        if "error" in a:
            out.append(f"| {a.get('competitor_name','?')} | "
                       f"{a.get('url','?')} | (error: {a['error']}) | | | | |")
            continue
        out.append(
            f"| {a.get('competitor_name','?')} "
            f"| {a.get('url','?')} "
            f"| {a.get('their_headline','?')[:80]} "
            f"| {a.get('their_pricing_tier','?')[:30]} "
            f"| {a.get('their_moat','?')[:80]} "
            f"| {a.get('our_differentiation','?')[:80]} "
            f"| {a.get('should_we_compete','?')} |"
        )
    return "\n".join(out)


def do_one():
    picked = pick_oldest("competitor-intel")
    if not picked:
        return False
    src_path, item = picked
    project = item.get("project") or item.get("target_project")
    if not project:
        # Skip items without project
        advance(item, src_path, "design", "competitor-intel",
                "no project; skipped")
        return True
    repo = PROJECTS_ROOT / project
    if not repo.exists():
        advance(item, src_path, "design", "competitor-intel",
                "no local repo; skipped (will run later)")
        return True

    bd = item.get("bd_verdict") or {}
    competitors = bd.get("incumbent_competitors") or []
    if not competitors:
        advance(item, src_path, "design", "competitor-intel",
                "no incumbent_competitors listed by BD")
        return True

    log("competitor-intel",
        f"▸ {item['id'][:32]} → analyzing {len(competitors)} competitors")

    analyses = []
    for c in competitors[:5]:   # cap 5 per item
        if _stop:
            break
        a = analyze_competitor(
            c,
            bd.get("new_product_one_liner") or bd.get("feature_one_liner",""),
            bd.get("buyer_persona", ""),
            bd.get("pricing_tier", ""),
        )
        if a:
            analyses.append(a)

    biz = repo / "business"
    biz.mkdir(exist_ok=True)
    (biz / "competitor-intel.md").write_text(render_intel_md(item, analyses))
    item["competitor_intel"] = analyses
    advance(item, src_path, "design", "competitor-intel",
            f"analyzed {len(analyses)} competitors")
    log("competitor-intel", f"  ✓ {item['id'][:32]} → design")
    return True


if __name__ == "__main__":
    daemon_loop("competitor-intel", POLL_SEC, do_one)
