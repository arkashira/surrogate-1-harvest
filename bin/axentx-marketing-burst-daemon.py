#!/usr/bin/env python3
"""axentx Marketing Burst — generate ready-to-post copy for every live product.

Reads top-products.json + live-urls.json. For each high-score live product,
generates:
  - tweet.txt        (280-char tweet with link)
  - reddit.md        (title + body for /r/SaaS, /r/SideProject)
  - hn.txt           (Hacker News submit title + URL)
  - devto.md         (Dev.to article skeleton ~500 words)
  - producthunt.md   (PH launch copy)
  - linkedin.md      (LinkedIn post)
  - email.md         (cold-outreach email template)

Output: /opt/surrogate-1-harvest/state/marketing/<slug>/<platform>.<ext>

Plus a single "marketing dashboard" HTML deployed to:
  https://axentx-marketing.pages.dev/  — browse + copy any piece in one click.

Cycle: 1h. Uses LLM for the longer Dev.to + Reddit body; tweet/HN are templated.
"""
from __future__ import annotations
import base64
import datetime
import html
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, call_llm, get_role_budget  # noqa: E402

try:
    import blake3 as _blake3
except ImportError:
    _blake3 = None

CYCLE_SEC = int(os.environ.get("MARKET_CYCLE_SEC", "3600"))
MIN_SCORE = int(os.environ.get("MARKET_MIN_SCORE", "30"))
MAX_PER_CYCLE = int(os.environ.get("MARKET_MAX_PER_CYCLE", "5"))
TOP = REPO_ROOT / "state" / "top-products.json"
LIVE = REPO_ROOT / "state" / "live-urls.json"
OUT_DIR = REPO_ROOT / "state" / "marketing"
ACCT = "77fb5e6c3716be794dc3e8467ba9f285"
PROJECT = "axentx-marketing"
LLM_BUDGET = get_role_budget("marketing", 1200)

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("marketing", "shutdown")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


PROMPT_DEVTO = """Write a Dev.to article about a new SaaS product. Format MARKDOWN.

PRODUCT: {name}
TAGLINE: {tagline}
URL: {url}
CATEGORY: {category}
RECENT FEATURES: {features}

Write 400-600 words, with these sections:
- Hook: a specific pain a developer faces today (concrete, with example).
- The fix: how this product solves it (no fluff).
- "Why now": why the problem is becoming acute (trends, regulations, etc).
- Try it: link, free tier, what to do in 5 minutes.

Tone: senior engineer writing for engineers. No marketing-speak. Show, don't tell.
End with a single CTA line + the link.

Output ONLY the article markdown — no preamble, no closing meta."""


PROMPT_REDDIT = """Write a Reddit post for /r/SaaS (or /r/SideProject) about this product.

PRODUCT: {name}
TAGLINE: {tagline}
URL: {url}
CATEGORY: {category}

Format:
TITLE: <80-char title that's CONCRETE and shows the BENEFIT, not the feature>

BODY:
<2-3 paragraphs, ~150 words. First-person ("I built…"). Be specific about the
problem you faced. Show what's free. End with: "Free to try at <URL>. Happy to
answer questions / take feedback."

No emojis. No exclamation marks. No "🚀 launching!". Reddit hates that.>

Output exactly:
TITLE: <line>

BODY:
<paragraphs>"""


PROMPT_LINKEDIN = """Write a LinkedIn post about this SaaS launch (~120 words).

PRODUCT: {name}
TAGLINE: {tagline}
URL: {url}

Tone: professional but human. Talk about the underlying problem first. Mention
the unexpected reason the product exists (e.g. "I was tired of legacy tooling").
Include 2-3 line breaks for readability. End with the URL on its own line.
No hashtag stuffing — max 3 relevant hashtags at the end."""


def _short_url(url):
    """Clean trailing slash for tweet brevity."""
    return url.rstrip("/") if url else ""


def _tweet(name, tagline, url, score):
    """Build a 280-char tweet without LLM."""
    base = f"🚀 {name} is live\n\n{tagline}\n\n{_short_url(url)}"
    if len(base) > 270:
        # Trim tagline
        budget = 270 - (len(name) + len(_short_url(url)) + 20)
        if budget > 30:
            tagline = tagline[:budget].rstrip(" .,") + "…"
        base = f"🚀 {name} is live\n\n{tagline}\n\n{_short_url(url)}"
    return base[:280]


def _hn_submit(name, tagline, url):
    """Hacker News title + URL. PG style: "Show HN:" works for self-built."""
    title = f"Show HN: {name} – {tagline}"
    if len(title) > 80:
        title = title[:77] + "…"
    return {"title": title, "url": _short_url(url)}


def _producthunt(name, tagline, url):
    """ProductHunt launch copy. PH expects:
       Tagline (≤60), Description (≤260)."""
    return f"""# {name}

**Tagline (60 char max):**
{tagline[:60]}

**Description (260 char max):**
{tagline}. Built and shipped autonomously by an AI engineering team —
new features ship daily. Free to start. Try it: {_short_url(url)}

**First comment:**
Hey everyone — {name} is one of {get_total_products()}+ products our autonomous
AI team has shipped. Real working code, not vibes. See {_short_url(url)} +
the live development feed at https://axentx.pages.dev/.

Free tier; happy to take feedback in the comments.
"""


def _email(name, tagline, url):
    return f"""Subject: A short note about {name}

Hi {{first_name}},

Quick one — I noticed {{company}} is in the {tagline.lower()[:30]}… space and
thought {name} might fit:

  • {tagline}
  • Free to start at {_short_url(url)}
  • 2-min onboard, no credit card

If it's a fit, the team and I would love your feedback in the first 14 days.
If not, no worries — happy to delete this thread.

— Ashira
"""


def get_total_products():
    try:
        return json.loads(TOP.read_text()).get("total_products", 30)
    except Exception:
        return 30


def _http(method, url, headers=None, data=None, timeout=60):
    h = {"User-Agent": "axentx-mkt/1"}
    if headers:
        h.update(headers)
    body = data
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _cf_token():
    r = subprocess.run(
        ["bash", "-c",
         "grep '^CLOUDFLARE_API_TOKEN=' /etc/surrogate-coordinator.env"
         " | cut -d= -f2-"],
        capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


def _ensure_project(token):
    s, b = _http(
        "POST",
        f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/pages/projects",
        headers={"Authorization": f"Bearer {token}"},
        data={"name": PROJECT, "production_branch": "main"})
    if s == 200 or s == 409 or "already exists" in b.lower() or "duplicate" in b.lower():
        return True
    log("marketing", f"  ⚠ ensure project: {s} {b[:200]}")
    return False


def _deploy_html(token, html_text):
    if _blake3 is None:
        return None
    content = html_text.encode()
    h = _blake3.blake3(content + b"html").hexdigest()[:32]
    manifest = {"/index.html": h}
    s, b = _http(
        "GET",
        f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
        f"/pages/projects/{PROJECT}/upload-token",
        headers={"Authorization": f"Bearer {token}"})
    d = json.loads(b)
    if not d.get("success"):
        return None
    jwt = d["result"]["jwt"]
    s, b = _http(
        "POST",
        "https://api.cloudflare.com/client/v4/pages/assets/check-missing",
        headers={"Authorization": f"Bearer {jwt}"},
        data={"hashes": [h]})
    d = json.loads(b)
    missing = d.get("result") if d.get("success") else None
    if missing is None:
        return None
    if missing:
        s, b = _http(
            "POST",
            "https://api.cloudflare.com/client/v4/pages/assets/upload",
            headers={"Authorization": f"Bearer {jwt}"},
            data=[{
                "key": h,
                "value": base64.b64encode(content).decode(),
                "metadata": {"contentType": "text/html"},
                "base64": True,
            }], timeout=120)
        if not json.loads(b).get("success"):
            return None
    boundary = f"----axentxMkt{int(time.time())}"
    parts = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="manifest"\r\n\r\n',
        json.dumps(manifest).encode(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    s, b = _http(
        "POST",
        f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
        f"/pages/projects/{PROJECT}/deployments",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        data=body, timeout=120)
    d = json.loads(b) if b else {}
    if d.get("success"):
        return d["result"].get("url")
    return None


def normalize_prod_url(url):
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        parts = u.hostname.split(".")
        if (len(parts) == 4 and parts[-2] == "pages"
                and parts[-1] == "dev" and len(parts[0]) in (8, 32)):
            return f"https://{'.'.join(parts[1:])}/"
        return url.rstrip("/") + "/"
    except Exception:
        return url


def get_features(slug, top_data):
    """Pull PENDING tags as feature names from top-products data."""
    for p in top_data.get("all_ranked", []):
        if p["slug"] == slug:
            n = p.get("signals", {}).get("pending_features", 0)
            return f"~{n} features in queue"
    return ""


def get_tagline(slug, top_data, fallback="AI-built SaaS for teams that ship"):
    for p in top_data.get("all_ranked", []):
        if p["slug"] == slug:
            t = (p.get("tagline") or "").strip()
            t = re.sub(r"<[^>]+>", "", t)
            t = re.sub(r"^[^\w]+", "", t)
            t = re.split(r"\s+·\s+", t)[0]
            if t and len(t) >= 15:
                return t[:140]
    return fallback


def generate_for_product(slug, url, score, top_data):
    name = slug.replace("-", " ").title()
    tagline = get_tagline(slug, top_data)
    features = get_features(slug, top_data)
    category = "platform"
    for p in top_data.get("all_ranked", []):
        if p["slug"] == slug:
            c = p.get("category") or "platform"
            if c != "uncategorized":
                category = c.replace("-", " ")
            break

    out = OUT_DIR / slug
    out.mkdir(parents=True, exist_ok=True)
    written = []

    # 1. Tweet (templated, no LLM)
    (out / "tweet.txt").write_text(_tweet(name, tagline, url, score))
    written.append("tweet.txt")

    # 2. HN
    hn = _hn_submit(name, tagline, url)
    (out / "hn.json").write_text(json.dumps(hn, indent=2))
    (out / "hn.txt").write_text(f"{hn['title']}\n{hn['url']}\n")
    written.append("hn.txt")

    # 3. ProductHunt
    (out / "producthunt.md").write_text(_producthunt(name, tagline, url))
    written.append("producthunt.md")

    # 4. Email
    (out / "email.md").write_text(_email(name, tagline, url))
    written.append("email.md")

    # 5. LLM-driven: Reddit + Dev.to + LinkedIn (only if newer than 12h)
    fresh = lambda p: not p.is_file() or (
        time.time() - p.stat().st_mtime > 12 * 3600)

    if fresh(out / "reddit.md"):
        try:
            txt = call_llm(
                PROMPT_REDDIT.format(name=name, tagline=tagline, url=url,
                                     category=category),
                system="You write Reddit posts that don't sound like ads.",
                max_tokens=LLM_BUDGET, timeout=60)
            if txt:
                (out / "reddit.md").write_text(txt.strip())
                written.append("reddit.md")
        except Exception as e:
            log("marketing", f"  ⚠ reddit LLM {slug}: {type(e).__name__}")

    if fresh(out / "devto.md"):
        try:
            txt = call_llm(
                PROMPT_DEVTO.format(name=name, tagline=tagline, url=url,
                                    category=category, features=features),
                system="You write technical articles for engineers.",
                max_tokens=LLM_BUDGET, timeout=60)
            if txt:
                (out / "devto.md").write_text(txt.strip())
                written.append("devto.md")
        except Exception as e:
            log("marketing", f"  ⚠ devto LLM {slug}: {type(e).__name__}")

    if fresh(out / "linkedin.md"):
        try:
            txt = call_llm(
                PROMPT_LINKEDIN.format(name=name, tagline=tagline, url=url),
                system="You write authentic LinkedIn posts.",
                max_tokens=600, timeout=60)
            if txt:
                (out / "linkedin.md").write_text(txt.strip())
                written.append("linkedin.md")
        except Exception as e:
            log("marketing", f"  ⚠ linkedin LLM {slug}: {type(e).__name__}")

    return written


def render_dashboard(top_data, live_urls):
    """HTML dashboard listing all products with their marketing assets."""
    rows = []
    for slug, info in sorted(live_urls.items(),
                              key=lambda x: -x[1].get("score", 0)):
        url = normalize_prod_url(info.get("url", "#"))
        score = info.get("score", 0)
        tagline = get_tagline(slug, top_data)
        out_dir = OUT_DIR / slug
        if not out_dir.is_dir():
            continue
        assets = []
        for f in sorted(out_dir.glob("*")):
            if f.is_file():
                try:
                    body = f.read_text()
                    body = body[:4000]
                except Exception:
                    body = ""
                assets.append((f.name, body))
        assets_html = "".join(
            f'<details><summary>{html.escape(name)} '
            f'<span class="copy-hint">click → reveal + copy</span></summary>'
            f'<pre>{html.escape(body)}</pre></details>'
            for name, body in assets)
        rows.append(
            f'<div class="card">'
            f'<div class="card-head">'
            f'<h3>{html.escape(slug.replace("-"," ").title())} '
            f'<span class="score">★ {score:.0f}</span></h3>'
            f'<a class="open" href="{html.escape(url)}" target="_blank">Open →</a>'
            f'</div>'
            f'<p class="tagline">{html.escape(tagline)}</p>'
            f'<div class="assets">{assets_html}</div>'
            f'</div>'
        )
    return MARKET_HTML.format(
        n=len(rows),
        rows="\n".join(rows),
        ts=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )


MARKET_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>axentx · marketing dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0a0e1a;--bg-2:#11172a;--card:#161b30;--fg:#e6e9f5;--muted:#8a91a8;--accent:#00e5ff;}}
body{{background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55}}
.wrap{{max-width:1100px;margin:0 auto;padding:30px 24px}}
h1{{font-size:30px;letter-spacing:-0.01em;margin-bottom:8px}}
.lead{{color:var(--muted);font-size:15px;margin-bottom:30px}}
.card{{background:var(--card);border:1px solid var(--bg-2);border-radius:12px;padding:18px;margin-bottom:14px}}
.card-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
h3{{font-size:18px}}
.score{{color:var(--accent);font-size:13px;margin-left:6px}}
.tagline{{color:var(--muted);font-size:14px;margin-bottom:12px}}
.open{{display:inline-block;padding:6px 12px;border-radius:8px;background:var(--accent);color:var(--bg);text-decoration:none;font-size:13px;font-weight:600}}
.open:hover{{filter:brightness(1.1)}}
details{{margin:8px 0;padding:8px 12px;border:1px solid var(--bg-2);border-radius:8px;cursor:pointer}}
details[open]{{background:var(--bg-2)}}
summary{{font-size:13px;color:var(--accent);user-select:none}}
.copy-hint{{color:var(--muted);font-size:11px;font-weight:400}}
pre{{background:#0c1124;padding:12px;border-radius:6px;overflow:auto;font-size:13px;line-height:1.4;color:var(--fg);margin-top:8px;white-space:pre-wrap;word-wrap:break-word;-webkit-user-select:all;user-select:all}}
footer{{color:var(--muted);font-size:13px;text-align:center;margin-top:30px}}
</style></head>
<body><div class="wrap">
<h1>marketing burst</h1>
<p class="lead">Ready-to-post copy for {n} live products. Click any asset → select-all, copy, paste. Generated {ts}.</p>
{rows}
<footer>auto-refreshed every 1h · <a style="color:var(--accent)" href="https://axentx.pages.dev/">products</a> · <a style="color:var(--accent)" href="https://axentx-status.pages.dev/">status</a></footer>
</div></body></html>
"""


def main():
    log("marketing",
        f"start — cycle={CYCLE_SEC}s, min_score={MIN_SCORE}, "
        f"max/cycle={MAX_PER_CYCLE} → axentx-marketing.pages.dev")
    if _blake3 is None:
        log("marketing", "✗ blake3 missing")
        return 1
    while not _stop:
        try:
            top_data = json.loads(TOP.read_text()) if TOP.is_file() else {}
            live_urls = json.loads(LIVE.read_text()) if LIVE.is_file() else {}
            ranked = top_data.get("all_ranked", [])
            elig = [(p["slug"], live_urls.get(p["slug"], {}).get("url"),
                     p.get("score", 0))
                    for p in ranked
                    if p.get("score", 0) >= MIN_SCORE
                    and p["slug"] in live_urls]
            elig = elig[:MAX_PER_CYCLE]
            log("marketing",
                f"▸ {len(elig)} eligible products with live URLs")

            for slug, url, score in elig:
                if _stop:
                    break
                if not url:
                    continue
                url = normalize_prod_url(url)
                written = generate_for_product(slug, url, score, top_data)
                log("marketing",
                    f"  ✓ {slug} (score={score:.0f}) → {len(written)} assets "
                    f"({', '.join(written)[:60]})")

            # Build + deploy dashboard
            token = _cf_token()
            if token and _ensure_project(token):
                page = render_dashboard(top_data, live_urls)
                url = _deploy_html(token, page)
                if url:
                    log("marketing",
                        f"✓ dashboard → {url} "
                        f"(prod: https://{PROJECT}.pages.dev/)")
        except Exception as e:
            log("marketing",
                f"⚠ cycle: {type(e).__name__}: {str(e)[:160]}")
        for _ in range(CYCLE_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
