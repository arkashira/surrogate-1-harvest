"""axentx_firecrawl — Firecrawl helper module.

Firecrawl: industry-leading web-scrape-to-clean-markdown API. Handles
JS rendering, rotating proxies, rate limits, JS-blocked content.
Replace fragile per-source regex/RSS parsers with one clean API call.

Usage in any daemon:
    from axentx_firecrawl import scrape, search, extract, crawl
    md = scrape("https://news.ycombinator.com")
    items = search("thai SaaS payroll 2026", limit=10)
    schema = extract(["https://x.com"], schema={...})

All functions return None on failure (never raise) so caller can fall
through to a backup scraper.
"""
from __future__ import annotations
import json
import os
import urllib.request
import urllib.error

FC_BASE = os.environ.get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev")
FC_KEY = os.environ.get("FIRECRAWL_API_KEY", "")


# Track whether Firecrawl credit pool is exhausted (402) or rate-limited
# hard (repeated 429). Once flipped, _post short-circuits → caller falls
# through to axentx_self_scrape. Reset on process restart only — daemon
# cycles are minutes apart, so a per-process flag is fine.
_FC_DEAD = False


def _post(path: str, body: dict, timeout: int = 60) -> dict | None:
    global _FC_DEAD
    if _FC_DEAD or not FC_KEY:
        return None
    try:
        req = urllib.request.Request(
            f"{FC_BASE}{path}",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {FC_KEY}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 402 = out of credits → permanently dead until restart
        # 401 = bad/expired key → also dead
        # 429 = rate limited → mark dead too (caller falls back; cheaper
        # than burning quota on a backoff dance)
        if e.code in (401, 402, 429):
            _FC_DEAD = True
        return None
    except (urllib.error.URLError, json.JSONDecodeError, Exception):
        return None


def _self_scrape_fallback(url: str, only_main: bool, timeout: int) -> str | None:
    """Lazy-import stdlib fallback so daemons that never use scrape()
    don't pay the import cost."""
    try:
        from axentx_self_scrape import scrape as _ss
        return _ss(url, only_main=only_main, timeout=timeout)
    except Exception:
        return None


def scrape(url: str, formats: list[str] | None = None,
           only_main: bool = True, timeout: int = 60) -> str | None:
    """Scrape one URL → clean markdown. Returns the markdown body or None.

    Falls through to axentx_self_scrape (pure-stdlib HTML→markdown) when
    Firecrawl is unavailable: missing key, out of credits (402), or
    rate-limited (429). Same return shape either way."""
    body = {"url": url, "formats": formats or ["markdown"],
            "onlyMainContent": only_main}
    r = _post("/v2/scrape", body, timeout=timeout)
    if r and r.get("success"):
        md = ((r.get("data") or {}).get("markdown")) or None
        if md:
            return md
    # Firecrawl unavailable or returned empty — try stdlib scraper
    return _self_scrape_fallback(url, only_main, timeout)


def scrape_full(url: str, formats: list[str] | None = None,
                timeout: int = 60) -> dict | None:
    """Same as scrape() but returns full data dict (markdown + html +
    metadata + links). Use when you need title, description, og:image."""
    body = {"url": url, "formats": formats or ["markdown", "html"]}
    r = _post("/v2/scrape", body, timeout=timeout)
    if not r or not r.get("success"):
        return None
    return r.get("data")


def search(query: str, limit: int = 10, timeout: int = 90) -> list[dict]:
    """Web search (Google-equivalent) → list of {title, url, description}.
    Use for keyword-driven discovery (replaces brittle Reddit/HN-search
    scrapers). Costs 1 credit / 5 results."""
    body = {"query": query, "limit": limit}
    r = _post("/v2/search", body, timeout=timeout)
    if not r or not r.get("success"):
        return []
    data = r.get("data") or {}
    if isinstance(data, dict):
        # v2 returns {web: [...], news: [...], images: [...]}
        return (data.get("web") or []) + (data.get("news") or [])
    return data if isinstance(data, list) else []


def extract(urls: list[str], schema: dict, prompt: str | None = None,
            timeout: int = 120) -> dict | None:
    """Structured extraction — give a JSON schema, get JSON back per url.
    Far more reliable than LLM-on-raw-html. Costs 1-3 credits / page.

    Example:
        schema = {"type":"object","properties":{
            "founder":{"type":"string"},
            "monetization":{"type":"string"},
            "tech_stack":{"type":"array","items":{"type":"string"}}}}
        out = extract(["https://startup.com"], schema)
    """
    body: dict = {"urls": urls, "schema": schema}
    if prompt:
        body["prompt"] = prompt
    r = _post("/v2/extract", body, timeout=timeout)
    if not r or not r.get("success"):
        return None
    return r.get("data")


def crawl(url: str, limit: int = 20, max_depth: int = 2,
          timeout: int = 180) -> list[dict] | None:
    """Multi-page crawl from a starting URL. Use for documentation sites,
    blogs, or competitor product pages. Returns list of {url, markdown}."""
    body = {"url": url, "limit": limit, "maxDepth": max_depth,
            "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True}}
    r = _post("/v2/crawl", body, timeout=timeout)
    if not r or not r.get("success"):
        return None
    # /v2/crawl returns a job id — caller could poll, but for now we
    # return the immediate response (sometimes contains data inline)
    return r.get("data") or [{"jobId": r.get("id"), "status": r.get("status")}]


# Convenience: drop-in replacement for legacy `urlopen → html → regex`
def fetch_clean(url: str) -> str | None:
    """Drop-in for `urllib.request.urlopen(url).read()` use cases that
    actually want clean readable text. Falls through to None if Firecrawl
    unavailable — caller can fall back to plain urllib."""
    return scrape(url)


__all__ = ["scrape", "scrape_full", "search", "extract", "crawl",
           "fetch_clean", "FC_KEY", "FC_BASE"]
