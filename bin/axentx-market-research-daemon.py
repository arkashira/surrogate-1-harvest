#!/usr/bin/env python3
"""axentx market-research — stream TAM/SAM/SOM per validated pain.

Pipeline slot: validator → market-research → bd
  (validated pains get a market lens BEFORE bd routes them, so bd can
   prioritize NEW-PRODUCT verdicts on opportunities with real demand)

Sources (all free, no API keys beyond HF_TOKEN):
  - DuckDuckGo HTML search (unauth, generous rate)
  - Wikipedia REST API (unauth)
  - SET (Stock Exchange of Thailand) public API (industry breakdown TH)
  - LLM synthesis to extract numerical TAM/SAM/SOM

Output payload added to item:
  market_data = {
    tam_global_usd, sam_global_usd, som_global_usd,
    tam_thailand_thb, demand_signal (low|med|high),
    competitors: [...], confidence: 0-1,
    rationale: "<why these numbers>",
    research_queries: [...]
  }

Flow: claim from market-research stage → web-search via DDG →
      Wikipedia for category context → LLM synth → write to item →
      advance to 'bd' stage.
"""
from __future__ import annotations
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, call_llm, pick_oldest, advance,  # noqa: E402
                             fail, daemon_loop, get_role_budget)

POLL_SEC = int(os.environ.get("MR_POLL_SEC", "30"))
MR_BUDGET = get_role_budget("market-research", 1500)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")


def _http_get(url: str, timeout: int = 15) -> str | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/json",
        "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def ddg_search(query: str, limit: int = 8) -> list[tuple[str, str]]:
    """DuckDuckGo HTML SERP scrape — returns [(title, snippet), ...]."""
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    html = _http_get(url)
    if not html:
        return []
    results: list[tuple[str, str]] = []
    for m in re.finditer(
        r'<a[^>]*class="result__a"[^>]*>([^<]+)</a>.*?'
        r'<a[^>]*class="result__snippet"[^>]*>([^<]+)</a>',
        html, re.DOTALL,
    ):
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        snippet = re.sub(r"<[^>]+>", "", m.group(2))
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if title and snippet:
            results.append((title, snippet))
        if len(results) >= limit:
            break
    return results


def wikipedia_summary(topic: str) -> str:
    url = (f"https://en.wikipedia.org/api/rest_v1/page/summary/"
           f"{urllib.parse.quote(topic)}")
    html = _http_get(url, timeout=10)
    if not html:
        return ""
    try:
        d = json.loads(html)
        return (d.get("extract") or "")[:1500]
    except Exception:
        return ""


MR_SYS = (
    "You are a market analyst sizing the addressable market (TAM/SAM/SOM) "
    "for a tech product hypothesis. Use only the search snippets provided + "
    "your knowledge. Output STRICT JSON, no prose. All amounts in USD, with "
    "millions/billions explicit (e.g. 12.5e9 for $12.5B)."
)
MR_USER_TMPL = (
    "Pain hypothesis: {hypothesis}\n\n"
    "Web search snippets (the most relevant 8 results):\n{snippets}\n\n"
    "Wikipedia summary of related category:\n{wiki}\n\n"
    "Output JSON shape:\n"
    '{{"tam_global_usd": <number>, "sam_global_usd": <number>, '
    '"som_global_usd": <number>, "tam_thailand_thb": <number or null>, '
    '"demand_signal": "low|med|high", "competitors": ["name1", "name2"], '
    '"confidence": <0..1>, "rationale": "<2-3 sentence why>"}}'
)


def research_market(hypothesis: str) -> dict:
    """Search → wiki → LLM synth → JSON."""
    queries = [
        f"{hypothesis[:80]} market size",
        f"{hypothesis[:80]} TAM SAM",
        f"{hypothesis[:80]} competitors landscape",
    ]
    snippets: list[str] = []
    for q in queries:
        for title, snip in ddg_search(q, limit=4):
            snippets.append(f"[{title[:80]}] {snip[:200]}")
        time.sleep(1.5)
    wiki_topic = " ".join(hypothesis.split()[:4])
    wiki = wikipedia_summary(wiki_topic)
    out = call_llm(
        MR_USER_TMPL.format(
            hypothesis=hypothesis[:400],
            snippets="\n".join(snippets[:12]) or "(no results)",
            wiki=wiki or "(none)"),
        system=MR_SYS, max_tokens=MR_BUDGET,
    )
    try:
        # Strip ```json fences if present
        out = out.strip()
        if out.startswith("```"):
            out = out.split("```", 2)[1]
            if out.startswith("json"):
                out = out[4:]
        data = json.loads(out)
        data["research_queries"] = queries
        return data
    except Exception as e:
        log("market-research", f"  json parse fail: {e}; raw={out[:200]!r}")
        return {
            "tam_global_usd": None, "demand_signal": "low",
            "confidence": 0.0, "rationale": f"parse-fail: {e}",
            "research_queries": queries,
        }


def do_one() -> bool:
    picked = pick_oldest("market-research")
    if not picked:
        return False
    src_path, item = picked
    bd_v = item.get("bd_verdict") or {}
    hyp = (bd_v.get("new_product_one_liner")
           or bd_v.get("feature_one_liner")
           or item.get("current", {}).get("text", "")
           or "")[:400]
    if not hyp.strip():
        fail(item, src_path, "market-research", "no hypothesis to size")
        return True
    log("market-research",
        f"▸ {item['id'][:32]} hypothesis: {hyp[:60]}")
    md = research_market(hyp)
    item["market_data"] = md
    log("market-research",
        f"  ✓ {md.get('demand_signal','?')} demand, "
        f"TAM=${(md.get('tam_global_usd') or 0)/1e9:.1f}B, "
        f"competitors={len(md.get('competitors', []) or [])}")
    advance(item, src_path, "bd", "market-research", json.dumps(md))
    return True


if __name__ == "__main__":
    daemon_loop("market-research", POLL_SEC, do_one)
