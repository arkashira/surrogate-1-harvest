#!/usr/bin/env python3
"""axentx social-pain stream — Mastodon hashtags + GitHub Discussions + Bluesky.

User direction round 2: 'ต่อๆ เอาเยอะๆ'

Real-time conversation pain from social platforms where founders/devs vent
in public. Mastodon and Bluesky are open — RSS/JSON without auth.
GitHub Discussions = repo-specific high-density pain.

Sources (10):
  1. Mastodon hashtag #saas
  2. Mastodon hashtag #buildinpublic
  3. Mastodon hashtag #indiehackers
  4. Mastodon hashtag #devops
  5. Mastodon hashtag #startup
  6. Mastodon hashtag #freelance
  7. Bluesky search "willing to pay" (via public AT Proto)
  8. Bluesky search "looking for tool"
  9. GitHub Discussions trending (top 10 repos by stars/discussions)
 10. Hacker Noon RSS (top tech blog)
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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, new_trace_id  # noqa: E402

CYCLE_GAP_SEC = float(os.environ.get("SOCIAL_CYCLE_GAP_SEC", "240"))
PER_REQ_GAP_SEC = float(os.environ.get("SOCIAL_REQ_GAP_SEC", "4.5"))
MAX_PER_SRC = int(os.environ.get("SOCIAL_MAX_PER_SRC", "15"))
MIN_TITLE_LEN = int(os.environ.get("SOCIAL_MIN_TITLE_LEN", "12"))

UA_POOL = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/120.0.0.0 Safari/537.36"),
]

CF_DEDUP_URL = os.environ.get(
    "CF_DEDUP_URL",
    "https://surrogate-1-cursor.ashira.workers.dev",
).rstrip("/")
_HOST = os.environ.get("HOSTNAME", "social-pain")
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("social-pain", "shutdown signal")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _ua():
    return random.choice(UA_POOL)


def _fp(url: str) -> str:
    return hashlib.md5(url.encode("utf-8", errors="replace")).hexdigest()[:16]


def _cf_seen_check(fps):
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


def _cf_seen_mark(fps):
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


def _http_get(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _ua(),
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,application/json;q=0.95,*/*;q=0.8"),
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw
    except Exception:
        return None


def _strip_tags(s):
    t = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t)).strip()


# ── Mastodon hashtag fetching (public Mastodon.social timelines API) ──
MASTODON_HASHTAGS = [
    ("saas",          "https://mastodon.social"),
    ("buildinpublic", "https://mastodon.social"),
    ("indiehackers",  "https://mastodon.social"),
    ("devops",        "https://mastodon.social"),
    ("startup",       "https://mastodon.social"),
    ("freelance",     "https://mastodon.social"),
    ("microsaas",     "https://hachyderm.io"),
    ("tech",          "https://hachyderm.io"),
]


def fetch_mastodon_hashtag(tag: str, instance: str) -> list[dict]:
    """Mastodon public timelines API — tag-based."""
    url = (f"{instance}/api/v1/timelines/tag/{tag}?limit={MAX_PER_SRC}"
           f"&local=false&only_media=false")
    raw = _http_get(url, timeout=12)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    posts = []
    for status in (data if isinstance(data, list) else [])[:MAX_PER_SRC]:
        if not isinstance(status, dict):
            continue
        content = _strip_tags(status.get("content") or "")
        if len(content) < 60:
            continue
        url = status.get("url") or status.get("uri", "")
        if not url:
            continue
        # First sentence as title
        first_sentence = re.split(r"[.!?]\s", content)[0][:200]
        if len(first_sentence) < MIN_TITLE_LEN:
            continue
        posts.append({
            "title": f"[Mastodon:#{tag}] {first_sentence}"[:500],
            "body": content[:6000],
            "url": url,
            "score": int(status.get("favourites_count") or 0),
            "source": f"mastodon:#{tag}",
        })
    return posts


# ── Bluesky search (public AT Proto) ──────────────────────────────────
BLUESKY_QUERIES = [
    "willing to pay",
    "looking for tool",
    "anyone built",
    "I would pay",
    "need a tool that",
    "saas idea",
]


def fetch_bluesky_search(query: str) -> list[dict]:
    """Bluesky public search via app.bsky.feed.searchPosts.
    No auth required for public posts."""
    q = urllib.parse.quote(query)
    url = (f"https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?"
           f"q={q}&limit={MAX_PER_SRC}&sort=top")
    raw = _http_get(url, timeout=12)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    posts = []
    for p in (data.get("posts") or [])[:MAX_PER_SRC]:
        if not isinstance(p, dict):
            continue
        record = p.get("record") or {}
        text = (record.get("text") or "").strip()
        if len(text) < 50:
            continue
        author = (p.get("author") or {}).get("handle") or "unknown"
        uri = p.get("uri", "")
        # Convert at:// URI to bsky.app URL
        m = re.match(r"at://(did:[^/]+)/[^/]+/([a-z0-9]+)", uri)
        if m:
            url = f"https://bsky.app/profile/{m.group(1)}/post/{m.group(2)}"
        else:
            url = uri
        if not url:
            continue
        first = text.split("\n")[0][:200]
        if len(first) < MIN_TITLE_LEN:
            continue
        posts.append({
            "title": f"[Bluesky:{query[:20]}] @{author}: {first}"[:500],
            "body": text[:6000],
            "url": url,
            "score": int((p.get("likeCount") or 0)
                         + (p.get("repostCount") or 0)),
            "source": f"bluesky:{query.replace(' ', '-')[:30]}",
        })
    return posts


# ── GitHub Discussions trending repos ─────────────────────────────────
GH_DISCUSSION_REPOS = [
    "vercel/next.js",
    "facebook/react",
    "microsoft/vscode",
    "kubernetes/kubernetes",
    "hashicorp/terraform",
    "golang/go",
    "python/cpython",
    "denoland/deno",
    "supabase/supabase",
    "open-webui/open-webui",
]


def fetch_gh_discussions(repo: str) -> list[dict]:
    """GitHub Discussions exposed via REST (rate-limited but works without
    auth for public repos)."""
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    headers = {
        "User-Agent": _ua(),
        "Accept": "application/vnd.github+json",
    }
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    # Use search API: /search/issues with type:discussion
    q = urllib.parse.quote(f"repo:{repo} type:discussion is:open")
    url = (f"https://api.github.com/search/issues?q={q}&"
           f"sort=updated&order=desc&per_page={MAX_PER_SRC}")
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    posts = []
    for it in (data.get("items") or [])[:MAX_PER_SRC]:
        title = (it.get("title") or "").strip()
        body = (it.get("body") or "")[:2000]
        url = it.get("html_url") or ""
        if not (title and url) or len(title) < MIN_TITLE_LEN:
            continue
        posts.append({
            "title": f"[GH-disc:{repo}] {title}"[:500],
            "body": body[:6000],
            "url": url,
            "score": int(it.get("reactions", {}).get("total_count") or 0),
            "source": f"gh-discussion:{repo.replace('/', '-')}",
        })
    return posts


# ── Hacker Noon top RSS ───────────────────────────────────────────────
def fetch_hackernoon():
    raw = _http_get("https://hackernoon.com/feed", timeout=12)
    if not raw:
        return []
    try:
        text = raw.decode("utf-8", errors="replace")
        text = re.sub(r"xmlns(:\w+)?\s*=\s*\"[^\"]+\"", "", text)
        text = re.sub(r"<(/?)\w+:", r"<\1", text)
        root = ET.fromstring(text)
    except Exception:
        return []
    posts = []
    for it in list(root.iter("item"))[:MAX_PER_SRC]:
        t = it.find("title")
        l = it.find("link")
        d = it.find("description") or it.find("content")
        if t is None or l is None:
            continue
        title = (t.text or "").strip()
        url = (l.text or "").strip()
        body = _strip_tags(d.text or "" if d is not None else "")[:3000]
        if len(title) < MIN_TITLE_LEN:
            continue
        posts.append({
            "title": f"[HackerNoon] {title}"[:500],
            "body": body[:6000],
            "url": url,
            "score": 0,
            "source": "hackernoon",
        })
    return posts


# ── orchestration ─────────────────────────────────────────────────────
SOURCES = (
    [(f"mastodon:{tag}",
      lambda t=tag, i=inst: fetch_mastodon_hashtag(t, i))
     for tag, inst in MASTODON_HASHTAGS]
    + [(f"bluesky:{q[:20]}", lambda q=q: fetch_bluesky_search(q))
       for q in BLUESKY_QUERIES]
    + [(f"gh-disc:{r}", lambda r=r: fetch_gh_discussions(r))
       for r in GH_DISCUSSION_REPOS]
    + [("hackernoon", fetch_hackernoon)]
)


def make_item(p: dict) -> dict:
    src = p["source"]
    # bluesky queries explicitly looking for "willing to pay" → high signal
    if any(k in src for k in ["bluesky:willing", "bluesky:I-would",
                              "bluesky:looking", "bluesky:need-a"]):
        sig, score = "high", 7
    elif "gh-disc" in src:
        sig, score = "medium", 5
    elif "mastodon" in src:
        sig, score = "medium", 4
    else:
        sig, score = "medium", 3
    item_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
        f"{p['source'].replace(':', '-').replace('/', '-')}-"
        f"{_fp(p['url'])}"
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
        "monetary_signal": sig,
        "monetary_intent_score": score,
        "history": [{
            "stage": "social-pain",
            "actor": "social-pain",
            "output": f"emit (sig={sig}, src={p['source']})",
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {
            "text": (f"[{p['source']}] {p['title']}\n\n"
                     f"{(p.get('body') or '')[:1500]}"),
        },
    }


def main() -> int:
    log("social-pain",
        f"streaming {len(SOURCES)} social sources "
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
                log("social-pain",
                    f"  {name} crashed: {type(e).__name__}: "
                    f"{str(e)[:80]}")
                continue
            if not posts:
                continue
            fps = [_fp(p["url"]) for p in posts]
            unseen = _cf_seen_check(fps)
            if unseen is None:
                unseen = set(fps)
            mark_now = []
            for p, fp in zip(posts, fps):
                if fp not in unseen:
                    skipped += 1
                    continue
                item = make_item(p)
                try:
                    write_item(item, "validator")
                    mark_now.append(fp)
                    emitted += 1
                    if emitted <= 3:
                        log("social-pain",
                            f"  ✓ {p['source']}: {p['title'][:75]}")
                except Exception as e:
                    log("social-pain",
                        f"  ✗ write: {type(e).__name__}: {str(e)[:60]}")
            if mark_now:
                _cf_seen_mark(mark_now)
            time.sleep(PER_REQ_GAP_SEC)
        elapsed = time.time() - cycle_start
        log("social-pain",
            f"cycle done — emitted={emitted}, skipped={skipped}, "
            f"elapsed={elapsed:.1f}s")
        remaining = max(0, CYCLE_GAP_SEC - elapsed)
        for _ in range(int(remaining)):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
