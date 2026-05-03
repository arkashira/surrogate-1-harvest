#!/usr/bin/env python3
"""axentx GitHub deep-stream — continuous harvest of pain→solution pairs.

Streams GitHub Issues + closed PRs across trending repos in target
domains. Every (issue_title, accepted_resolution) pair is a real-world
pain→solution signal that beats LLM-hallucinated training data.

Sources:
  - GET /search/issues?q=is:open label:bug stars:>100 (recent pains)
  - GET /search/issues?q=is:closed type:pr is:merged sort:updated
    (closed-PR commits with body "fixes #N" → resolution mapping)
  - GET /repos/{owner}/{repo}/issues?state=closed&labels=bug
    (per trending repo, closed bug issues with full discussion)

Anti-rate-limit:
  - GH_TOKEN_POOL — 4-12 PATs in rotation (see ~/.note GITHUB_TOKEN_POOL),
    each 5K req/h authenticated → 20K-60K req/h aggregate
  - Per-token X-RateLimit-Remaining tracked; cool down at <=100
  - Search API has 30 req/min hard limit (all tokens share quota for
    /search), so search calls are throttled at PER_SEARCH_GAP_SEC

Output: research-queue items with verified pain signal (real bug,
discussed by N users, resolved with M code changes).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, new_trace_id  # noqa: E402

# ── tunables ──────────────────────────────────────────────────────────────
SEARCH_QUERIES = os.environ.get(
    "GH_DEEP_QUERIES",
    "is:issue is:open label:bug stars:>500 sort:updated;"
    "is:issue is:open label:enhancement stars:>500 sort:reactions-+1-desc;"
    "is:issue is:closed reason:completed label:bug stars:>200 sort:updated;"
    "is:pr is:merged sort:updated stars:>200",
).split(";")

PER_SEARCH_GAP_SEC = float(os.environ.get("GH_SEARCH_GAP_SEC", "2.5"))
CYCLE_GAP_SEC = float(os.environ.get("GH_CYCLE_GAP_SEC", "60"))
PER_QUERY_PAGES = int(os.environ.get("GH_PAGES_PER_QUERY", "2"))

GH_TOKENS = [t.strip() for t in (
    os.environ.get("GITHUB_TOKEN_POOL", "")
    or os.environ.get("GH_TOKEN", "")
    or os.environ.get("GITHUB_TOKEN", "")
).split(",") if t.strip()]

# Supabase
SB_URL = os.environ.get(
    "SUPABASE_URL", "https://riunimyxoalicbntogbp.supabase.co",
).rstrip("/")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
SB_HEADERS = {
    "apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
    "Content-Type": "application/json",
}

UA = "axentx-github-deep-stream/1.0"

_stop = False
_token_cooldown: dict[str, float] = {}


def _on_signal(*_):
    global _stop
    _stop = True
    log("gh-deep", "shutdown signal")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


# ── Supabase ──────────────────────────────────────────────────────────────
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
    except Exception as e:
        log("gh-deep",
            f"  sb {method}: {type(e).__name__}: {str(e)[:120]}")
        return None


def already_seen(fp: str) -> bool:
    """seen_check_bulk RPC returns rows for fps that ARE seen
    (contract: `[{"fp": "..."}]` for found, `[]` for not found).
    Earlier `r[0].get("seen", False)` was always False because RPC
    never emits a 'seen' field — same root cause as Reddit stream
    duplicate emit."""
    r = _sb("POST", "rpc/seen_check_bulk", {
        "p_kind": "pain-url", "p_fps": [fp],
    })
    return isinstance(r, list) and len(r) > 0


def stamp_seen(fp: str) -> None:
    _sb("POST", "rpc/seen_mark_bulk", {
        "p_kind": "pain-url", "p_fps": [fp], "p_host": "gh-deep-stream",
    })


# ── GitHub ────────────────────────────────────────────────────────────────
def pick_token() -> str | None:
    if not GH_TOKENS:
        return None
    now = time.time()
    fresh = [t for t in GH_TOKENS if _token_cooldown.get(t, 0) <= now]
    if fresh:
        return random.choice(fresh)
    # all in cooldown → least-blocked
    return min(GH_TOKENS, key=lambda t: _token_cooldown.get(t, 0))


def gh_get(url: str, max_retries: int = 2) -> dict | list | None:
    for attempt in range(max_retries + 1):
        token = pick_token()
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": UA,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"token {token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                # Save rate-limit info per-token
                remaining = int(r.headers.get("X-RateLimit-Remaining", "9999"))
                reset = int(r.headers.get("X-RateLimit-Reset", "0"))
                if token and remaining < 100:
                    _token_cooldown[token] = reset
                    log("gh-deep",
                        f"  ⏸ token cool until reset (remaining={remaining})")
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 403 and "rate limit" in str(e.read())[:200].lower():
                if token:
                    _token_cooldown[token] = time.time() + 600
                continue
            if e.code == 422:
                # bad query — give up
                return None
            log("gh-deep", f"  HTTP {e.code} {url[:80]}")
            return None
        except Exception as e:
            log("gh-deep",
                f"  {type(e).__name__}: {str(e)[:120]} {url[:80]}")
            time.sleep(1)
    return None


def emit_issue_as_pain(item: dict) -> bool:
    """Issue → research-queue item."""
    url = item.get("html_url", "")
    title = (item.get("title") or "").strip()
    body = (item.get("body") or "").strip()
    reactions = item.get("reactions", {}) or {}
    upvotes = int(reactions.get("+1", 0) or 0)
    comments = int(item.get("comments", 0) or 0)
    state = item.get("state", "")
    state_reason = item.get("state_reason", "")

    if not (url and title) or len(title) < 15:
        return False

    fp = hashlib.sha1(url.encode()).hexdigest()[:16]
    if already_seen(fp):
        return False

    # Quality gate: meaningful engagement
    if upvotes + comments < 3:
        # Low signal — stamp seen so we don't fetch again, but skip emit
        stamp_seen(fp)
        return False

    discovery_id = new_trace_id()
    ts_iso = datetime.datetime.utcnow().isoformat() + "Z"
    item_id = (f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
               f"-gh-{fp}")

    pipeline_item = {
        "id": item_id,
        "discovery_id": discovery_id,
        "trace_id": discovery_id,
        "stage": "research",
        "created_at": ts_iso,
        "post": {
            "title": title,
            "body": body[:4000],
            "url": url,
            "score": upvotes + comments,
            "comments": comments, "upvotes": upvotes,
            "state": state, "state_reason": state_reason,
            "labels": [l.get("name") for l in (item.get("labels") or [])
                       if isinstance(l, dict)][:8],
        },
        "history": [{
            "stage": "research", "actor": "axentx-github-deep",
            "output": json.dumps({
                "title": title[:200], "url": url,
                "engagement": upvotes + comments,
                "state": state,
            }, ensure_ascii=False),
            "at": ts_iso,
        }],
        "current": {"text": f"[github-issue] {title}\n\n{body[:2000]}"},
    }
    # Write to validator-queue directly (research is a source stage
    # with no consumer; was piling up 2747 items unprocessed).
    pipeline_item["stage"] = "validator"
    write_item(pipeline_item, "validator")
    stamp_seen(fp)
    log("gh-deep",
        f"  ✓ pain (👍{upvotes} 💬{comments} {state}): {title[:70]}")
    return True


def fetch_search(query: str, page: int = 1) -> list[dict]:
    q = urllib.parse.quote(query) if hasattr(urllib, "parse") else query
    # Issues + PRs share the search/issues endpoint
    url = (f"https://api.github.com/search/issues?q={q}"
           f"&per_page=50&page={page}&sort=updated")
    d = gh_get(url)
    if not isinstance(d, dict):
        return []
    return d.get("items", []) or []


def main() -> int:
    if not SB_KEY:
        log("gh-deep", "FATAL: SUPABASE_SECRET_KEY not set")
        return 1
    if not GH_TOKENS:
        log("gh-deep",
            "⚠ no GH_TOKEN_POOL/GH_TOKEN — using unauth (60 req/h limit)")
    log("gh-deep",
        f"streaming with {len(GH_TOKENS)} token(s), "
        f"{len(SEARCH_QUERIES)} queries, gap={PER_SEARCH_GAP_SEC}s")

    import urllib.parse  # noqa: F401  needed by fetch_search

    while not _stop:
        cycle_start = time.time()
        emitted = 0
        for query in SEARCH_QUERIES:
            if _stop:
                break
            for page in range(1, PER_QUERY_PAGES + 1):
                if _stop:
                    break
                items = fetch_search(query, page=page)
                for it in items:
                    if emit_issue_as_pain(it):
                        emitted += 1
                time.sleep(PER_SEARCH_GAP_SEC)
        elapsed = time.time() - cycle_start
        log("gh-deep",
            f"cycle done — emitted {emitted} new pains in {elapsed:.0f}s")
        nap = max(0, CYCLE_GAP_SEC - elapsed)
        for _ in range(int(nap)):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
