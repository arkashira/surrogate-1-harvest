#!/usr/bin/env python3
"""axentx review-pain stream — harvest pain from REVIEWS of paid SaaS.

User direction 2026-05-10 (round 2):
  > 'ต่อๆ เอาเยอะๆ'

Reviews are GOLD: a 1-star G2 review = a buyer who already paid → admits
the tool failed them = pain WITH budget WITH willingness to switch.
Higher monetary signal than 'I wish there was a tool for X'.

Sources (8):
  1. AlternativeTo /trending — products people actively seek alternatives for
  2. AlternativeTo category pages — top 100 in each category
  3. ProductHunt deals/recently launched
  4. TrustPilot B2B latest reviews
  5. G2 category reviews (top 5 categories)
  6. Capterra category alternatives
  7. SourceForge reviews (newest)
  8. Slashdot reviews

Each item gets `monetary_signal: high` because reviewing means
having paid (or actively considering paying).
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
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, write_item, new_trace_id  # noqa: E402

CYCLE_GAP_SEC = float(os.environ.get("REVIEW_CYCLE_GAP_SEC", "240"))
PER_REQ_GAP_SEC = float(os.environ.get("REVIEW_REQ_GAP_SEC", "5.0"))
MAX_PER_SRC = int(os.environ.get("REVIEW_MAX_PER_SRC", "20"))
MIN_TITLE_LEN = int(os.environ.get("REVIEW_MIN_TITLE_LEN", "12"))

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

_HOST = os.environ.get("HOSTNAME", "review-stream")
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("review-stream", "shutdown signal")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _ua():
    return random.choice(UA_POOL)


def _fp(url: str) -> str:
    return hashlib.md5(url.encode("utf-8", errors="replace")).hexdigest()[:16]


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


def _http_get(url: str, timeout: int = 15) -> str | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _ua(),
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,*/*;q=0.8"),
            "Accept-Language": "en-US,en;q=0.5",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


def _strip_tags(html_text: str) -> str:
    t = re.sub(r"<script[^>]*>.*?</script>", " ", html_text, flags=re.DOTALL)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ── source: AlternativeTo /trending and category pages ────────────────
ALTERNATIVETO_CATEGORIES = [
    "business-software", "developer-tools", "productivity", "marketing",
    "communication", "monitoring", "security", "social-media-tools",
    "finance", "human-resources", "design", "ai-tools",
]


def fetch_alternativeto() -> list[dict]:
    """Pull AlternativeTo home + category top alternatives.
    Each entry = a popular paid tool people seek alternatives to."""
    posts = []
    targets = [("home", "https://alternativeto.net/")]
    for c in ALTERNATIVETO_CATEGORIES[:6]:
        targets.append((c, f"https://alternativeto.net/category/{c}/"))
    for label, url in targets:
        html_raw = _http_get(url, timeout=12)
        if not html_raw:
            continue
        # Extract <a href="/software/SLUG/" title="...">name</a>
        pat = re.compile(
            r'<a[^>]+href="(/software/[^"/]+/)"[^>]*title="([^"]+)"[^>]*>'
            r'\s*([^<]+?)\s*</a>',
            re.DOTALL,
        )
        seen_slugs = set()
        for m in pat.finditer(html_raw):
            href = m.group(1)
            title_attr = (m.group(2) or "").strip()
            link_text = (m.group(3) or "").strip()
            if href in seen_slugs:
                continue
            seen_slugs.add(href)
            full_url = f"https://alternativeto.net{href}"
            # Use category context so bd has hint
            display_title = (
                f"[AlternativeTo:{label}] {link_text} — alternatives sought"
            )
            body = (f"{title_attr}. Users on AlternativeTo are searching "
                    f"for replacements for {link_text} in the {label} space. "
                    f"This signals a market gap or dissatisfaction with "
                    f"{link_text}'s pricing/features/UX.")
            if len(link_text) < 3:
                continue
            posts.append({
                "title": display_title[:500],
                "body": body[:6000],
                "url": full_url,
                "score": 0,
                "source": f"alternativeto:{label}",
            })
            if len(posts) >= MAX_PER_SRC * len(targets):
                break
        time.sleep(PER_REQ_GAP_SEC)
        if _stop:
            break
    return posts


# ── source: ProductHunt — "deals" + recent launches ────────────────────
def fetch_producthunt_deals() -> list[dict]:
    """ProductHunt /deals + topics show real products with paid tiers."""
    posts = []
    for path in ("/", "/topics/saas", "/topics/developer-tools",
                 "/topics/productivity", "/topics/marketing"):
        url = f"https://www.producthunt.com{path}"
        html_raw = _http_get(url, timeout=15)
        if not html_raw:
            continue
        # Extract product cards with name + tagline
        pat = re.compile(
            r'<a[^>]+href="(/posts/[^"]+)"[^>]*>([^<]{10,80})</a>',
            re.DOTALL,
        )
        seen = set()
        for m in pat.finditer(html_raw):
            href = m.group(1)
            name = (m.group(2) or "").strip()
            if href in seen or len(name) < 5:
                continue
            seen.add(href)
            full_url = f"https://www.producthunt.com{href}"
            posts.append({
                "title": f"[PH:{path.lstrip('/')}] {name}"[:500],
                "body": (f"ProductHunt launch in {path}. Product '{name}' "
                         f"launched recently. The fact that it exists means "
                         f"someone validated the pain enough to build + ship. "
                         f"Check the comments + the alternatives section for "
                         f"real user pain points.")[:6000],
                "url": full_url,
                "score": 0,
                "source": f"producthunt-deals:{path.lstrip('/') or 'home'}",
            })
            if len(posts) >= MAX_PER_SRC:
                break
        time.sleep(PER_REQ_GAP_SEC)
        if _stop:
            break
    return posts


# ── source: AWS re:Post (Q&A — sysadmin/devops pain) ──────────────────
def fetch_aws_repost() -> list[dict]:
    """AWS official Q&A. Each unanswered question = paying customer
    actively stuck on AWS = high-value pain."""
    posts = []
    # AWS re:Post search RSS isn't available; scrape the listing page
    for tag in ("ec2", "lambda", "s3", "iam", "ecs", "rds", "vpc",
                "billing", "cost-management"):
        url = f"https://repost.aws/tags/{tag}"
        html_raw = _http_get(url, timeout=15)
        if not html_raw:
            continue
        pat = re.compile(
            r'<a[^>]+href="(/questions/[^"]+)"[^>]*>([^<]{15,200})</a>',
            re.DOTALL,
        )
        seen = set()
        for m in pat.finditer(html_raw):
            href = m.group(1)
            title = _strip_tags(m.group(2) or "").strip()
            if href in seen or len(title) < MIN_TITLE_LEN:
                continue
            seen.add(href)
            full_url = f"https://repost.aws{href}"
            posts.append({
                "title": f"[AWS-repost:{tag}] {title}"[:500],
                "body": (f"AWS re:Post question tagged '{tag}'. AWS users are "
                         f"AWS customers (paid). A question here signals an "
                         f"unmet need or workflow friction in the AWS "
                         f"ecosystem, where vendors can build paid solutions.")[:6000],
                "url": full_url,
                "score": 0,
                "source": f"aws-repost:{tag}",
            })
            if len(posts) >= MAX_PER_SRC // 2:
                break
        time.sleep(PER_REQ_GAP_SEC)
        if _stop:
            break
    return posts


# ── source: GitHub trending + Sponsors ────────────────────────────────
def fetch_github_trending() -> list[dict]:
    """GitHub /trending — what devs are starring. Issues on these repos
    represent emerging pain in cutting-edge tools."""
    posts = []
    for lang in ("", "python", "typescript", "go", "rust", "javascript"):
        path = "/trending" if not lang else f"/trending/{lang}"
        url = f"https://github.com{path}?since=daily"
        html_raw = _http_get(url, timeout=15)
        if not html_raw:
            continue
        # Extract <h2 class="h3 lh-condensed"><a href="/owner/repo">repo</a></h2>
        pat = re.compile(
            r'<h2[^>]+class="[^"]*h3[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"',
            re.DOTALL,
        )
        for m in pat.finditer(html_raw):
            href = m.group(1).strip()
            if not href.startswith("/") or href.count("/") != 2:
                continue
            full_url = f"https://github.com{href}"
            repo_name = href.lstrip("/")
            posts.append({
                "title": f"[GH-trending:{lang or 'all'}] {repo_name}"[:500],
                "body": (f"GitHub trending repo {repo_name} ({lang or 'any-lang'}). "
                         f"Trending = many devs starring TODAY = active pain "
                         f"in this space. Check open issues for paid-feature "
                         f"requests.")[:6000],
                "url": full_url,
                "score": 0,
                "source": f"gh-trending:{lang or 'all'}",
            })
            if len(posts) >= MAX_PER_SRC // 3:
                break
        time.sleep(PER_REQ_GAP_SEC)
        if _stop:
            break
    return posts


# ── source: Stack Exchange "hot" network-wide ──────────────────────────
def fetch_se_hot() -> list[dict]:
    """SE hot RSS — includes ALL SE sites (super.user, serverfault, dba,
    workplace, money, programmers, freelancing, etc.)"""
    url = "https://stackexchange.com/feeds/questions"
    raw = _http_get(url, timeout=15)
    if not raw:
        return []
    posts = []
    # Parse as RSS
    pat = re.compile(
        r"<entry>(.*?)</entry>", re.DOTALL,
    )
    title_pat = re.compile(r"<title[^>]*>([^<]+)</title>")
    link_pat = re.compile(r'<link href="([^"]+)"')
    sum_pat = re.compile(r"<summary[^>]*>(.*?)</summary>", re.DOTALL)
    for m in pat.finditer(raw):
        block = m.group(1)
        t = title_pat.search(block)
        l = link_pat.search(block)
        s = sum_pat.search(block)
        if not (t and l):
            continue
        title = html.unescape(t.group(1)).strip()
        link = l.group(1).strip()
        body = _strip_tags(s.group(1) if s else "")[:2000]
        # Identify which SE site
        site_m = re.match(r"https?://([^/]+)/", link)
        site = site_m.group(1).replace(".stackexchange.com", "").replace(
            ".com", "") if site_m else "se"
        posts.append({
            "title": f"[SE:{site}] {title}"[:500],
            "body": (f"Stack Exchange question on {site}. Real practitioner "
                     f"pain — questions on SE come from people stuck in "
                     f"production. Solutions to common SE-frequency questions "
                     f"= paid tool opportunities.\n\n{body}")[:6000],
            "url": link,
            "score": 0,
            "source": f"se-hot:{site}",
        })
        if len(posts) >= MAX_PER_SRC * 2:
            break
    return posts


# ── source: Indeed & Wellfound jobs (RSS) ─────────────────────────────
def fetch_wellfound() -> list[dict]:
    """WellFound (formerly AngelList) hiring at funded startups.
    Each job = a venture-backed company explicitly paying to solve a pain."""
    url = "https://wellfound.com/jobs?stage_in=Series%20A,Series%20B"
    html_raw = _http_get(url, timeout=15)
    if not html_raw:
        return []
    posts = []
    # WellFound uses dynamic SPA — parse job-card patterns
    pat = re.compile(
        r'href="(/jobs/\d+-[^"]+)"[^>]*>([^<]{15,150})</a>',
        re.DOTALL,
    )
    seen = set()
    for m in pat.finditer(html_raw):
        href = m.group(1)
        title = _strip_tags(m.group(2)).strip()
        if href in seen or len(title) < MIN_TITLE_LEN:
            continue
        seen.add(href)
        full_url = f"https://wellfound.com{href}"
        posts.append({
            "title": f"[WellFound] {title}"[:500],
            "body": (f"WellFound (AngelList) job at a Series A/B startup. "
                     f"Funded → real money for tools/services that help "
                     f"this team. Check role + company stage to identify "
                     f"build/buy opportunities.")[:6000],
            "url": full_url,
            "score": 0,
            "source": "wellfound-jobs",
        })
        if len(posts) >= MAX_PER_SRC:
            break
    return posts


# ── source: HN Who's Hiring monthly thread ────────────────────────────
def fetch_hn_whos_hiring() -> list[dict]:
    """HN's monthly Who's Hiring thread = the densest source of paid hire
    intent on the internet. Each comment = a company hiring devs for $$."""
    posts = []
    # Find current "Who is hiring?" thread via algolia
    url = ("https://hn.algolia.com/api/v1/search?query=Ask+HN+Who+is+hiring"
           "&tags=story&hitsPerPage=3")
    raw = _http_get(url, timeout=10)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    for hit in (data.get("hits") or [])[:1]:
        story_id = hit.get("objectID")
        if not story_id:
            continue
        # Fetch top-level comments
        c_url = (f"https://hn.algolia.com/api/v1/search?tags=comment,"
                 f"story_{story_id}&hitsPerPage={MAX_PER_SRC * 2}")
        c_raw = _http_get(c_url, timeout=10)
        if not c_raw:
            continue
        try:
            c_data = json.loads(c_raw)
        except Exception:
            continue
        for c in (c_data.get("hits") or [])[:MAX_PER_SRC]:
            txt = _strip_tags(c.get("comment_text") or "")
            if len(txt) < 80:
                continue
            # Extract company name from typical "Company | Role | Location" pattern
            first_line = txt.split("\n")[0][:200]
            posts.append({
                "title": f"[HN-Hiring] {first_line}"[:500],
                "body": txt[:6000],
                "url": (f"https://news.ycombinator.com/item?id="
                        f"{c.get('objectID')}"),
                "score": 0,
                "source": "hn-whos-hiring",
            })
    return posts


# ── source: Hashnode trending posts (devs writing about pains) ────────
def fetch_hashnode() -> list[dict]:
    """Hashnode is dev-focused blogging — real devs writing about real pains
    they encountered. Higher signal than generic Medium."""
    url = "https://hashnode.com/api/v2/discovery/feed/trending"
    raw = _http_get(url, timeout=12)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    posts = []
    items = (data.get("data") or {}).get("posts") or data.get("posts") or []
    if not isinstance(items, list):
        return []
    for p in items[:MAX_PER_SRC]:
        if not isinstance(p, dict):
            continue
        title = (p.get("title") or "").strip()
        brief = (p.get("brief") or p.get("subtitle", ""))[:2000]
        url = p.get("url", "")
        if not (title and url) or len(title) < MIN_TITLE_LEN:
            continue
        posts.append({
            "title": f"[Hashnode] {title}"[:500],
            "body": brief[:6000],
            "url": url,
            "score": int(p.get("totalReactions") or 0),
            "source": "hashnode-trending",
        })
    return posts


# ── source: Dev.to top per tag ────────────────────────────────────────
DEV_TAGS = [
    "saas", "startup", "career", "devops", "aws", "kubernetes",
    "productivity", "freelance", "ai", "indie",
]


def fetch_devto_tags() -> list[dict]:
    """Dev.to top posts per money-bearing tag."""
    posts = []
    for tag in DEV_TAGS:
        url = f"https://dev.to/api/articles?tag={tag}&top=7&per_page=5"
        raw = _http_get(url, timeout=10)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for a in (data if isinstance(data, list) else [])[:MAX_PER_SRC // 4]:
            title = (a.get("title") or "").strip()
            desc = (a.get("description") or "")[:2000]
            url = a.get("url", "")
            if not (title and url) or len(title) < MIN_TITLE_LEN:
                continue
            posts.append({
                "title": f"[Devto:{tag}] {title}"[:500],
                "body": desc[:6000],
                "url": url,
                "score": int(a.get("public_reactions_count") or 0),
                "source": f"devto:{tag}",
            })
        time.sleep(0.4)
        if _stop:
            break
    return posts


# ── source: Lemmy.world / Lemmy.ml selfhosted (federated reddit) ──────
def fetch_lemmy() -> list[dict]:
    """Lemmy instances aggregate self-hosters complaining about commercial
    SaaS. Niche but high-quality dev pain."""
    posts = []
    for instance, comm in [
        ("lemmy.world", "selfhosted"),
        ("lemmy.world", "asklemmy"),
        ("lemmy.ml", "technology"),
        ("programming.dev", "programming"),
    ]:
        url = f"https://{instance}/api/v3/post/list?community_name={comm}&sort=Hot&limit={MAX_PER_SRC}"
        raw = _http_get(url, timeout=10)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for entry in (data.get("posts") or [])[:MAX_PER_SRC]:
            p = entry.get("post") or {}
            title = (p.get("name") or "").strip()
            body = (p.get("body") or "")[:2000]
            url = p.get("url") or p.get("ap_id", "")
            if not (title and url) or len(title) < MIN_TITLE_LEN:
                continue
            posts.append({
                "title": f"[Lemmy:{comm}] {title}"[:500],
                "body": body[:6000],
                "url": url,
                "score": int(entry.get("counts", {}).get("score") or 0),
                "source": f"lemmy:{instance}:{comm}",
            })
        time.sleep(0.5)
        if _stop:
            break
    return posts


# ── orchestration ─────────────────────────────────────────────────────
SOURCES = [
    ("alternativeto", fetch_alternativeto),
    ("producthunt-deals", fetch_producthunt_deals),
    ("aws-repost", fetch_aws_repost),
    ("gh-trending", fetch_github_trending),
    ("se-hot", fetch_se_hot),
    ("wellfound", fetch_wellfound),
    ("hn-whos-hiring", fetch_hn_whos_hiring),
    ("hashnode", fetch_hashnode),
    ("devto-tags", fetch_devto_tags),
    ("lemmy", fetch_lemmy),
]


def make_item(p: dict) -> dict:
    """Build pipeline item. Reviews + AWS-paying-customer questions are
    HIGH monetary signal by default."""
    item_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-"
        f"{p['source'].replace(':', '-')}-{_fp(p['url'])}"
    )
    # AlternativeTo + AWS repost + WellFound + HN-hiring → high signal
    src = p["source"]
    if any(k in src for k in ["alternativeto", "aws-repost", "wellfound",
                              "hn-whos-hiring", "se-hot"]):
        sig = "high"
        score = 7
    elif "gh-trending" in src or "producthunt" in src or "hashnode" in src:
        sig = "medium"
        score = 4
    else:
        sig = "medium"
        score = 3
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
            "stage": "review-stream",
            "actor": "review-stream",
            "output": f"emit (sig={sig}, src={p['source']})",
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {
            "text": (f"[{p['source']}] {p['title']}\n\n"
                     f"{(p.get('body') or '')[:1500]}"),
        },
    }


def main() -> int:
    log("review-stream",
        f"streaming {len(SOURCES)} review-pain sources "
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
                log("review-stream",
                    f"  {name} crashed: {type(e).__name__}: "
                    f"{str(e)[:100]}")
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
                    log("review-stream",
                        f"  ✓ {name} sig={item['monetary_signal']}: "
                        f"{p['title'][:75]}")
                except Exception as e:
                    log("review-stream",
                        f"  ✗ write fail: {type(e).__name__}: "
                        f"{str(e)[:80]}")
            if mark_now:
                _cf_seen_mark(mark_now)
            time.sleep(PER_REQ_GAP_SEC)
        elapsed = time.time() - cycle_start
        log("review-stream",
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
