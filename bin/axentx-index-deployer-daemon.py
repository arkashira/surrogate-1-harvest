#!/usr/bin/env python3
"""axentx Index-Page Deployer — single aggregator landing of all live products.

User direction 2026-05-11:
  > 'product ที่เป็นที่ต้องการ มากที่สุด live ได้เร็วที่สุด'

A discovery URL pointing at the entire portfolio. Visitors hit ONE page
and see the full catalog of autonomously-shipped products.

Reads:
  /opt/surrogate-1-harvest/state/top-products.json  (rankings + signals)
  /opt/surrogate-1-harvest/state/live-urls.json     (deployed URLs)

Renders + deploys to Pages project `axentx` → axentx.pages.dev/

Cycle: 15 min (faster than per-product, since it aggregates all changes).
"""
from __future__ import annotations
import base64
import datetime
import html
import json
import os
import signal
import subprocess
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

try:
    import blake3 as _blake3
except ImportError:
    _blake3 = None

CYCLE_SEC = int(os.environ.get("INDEX_CYCLE_SEC", "900"))
TOP = REPO_ROOT / "state" / "top-products.json"
LIVE = REPO_ROOT / "state" / "live-urls.json"
DASHBOARD = REPO_ROOT / "state" / "dashboard.json"
ACCT = "77fb5e6c3716be794dc3e8467ba9f285"
PROJECT = "axentx"
AXENTX_BASE = Path("/opt/axentx")

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("index-deploy", "shutdown")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>axentx — autonomous AI ships {n_live} products (and counting)</title>
<meta name="description" content="An autonomous AI engineering team that ships SaaS products 24/7. {n_live} products live, {n_commits_24h} commits last 24h, {n_total} in pipeline.">
<meta property="og:title" content="axentx — autonomous AI ships {n_live} products">
<meta property="og:description" content="{n_live} products live · {n_commits_24h} commits / 24h · live development feed">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary_large_image">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --bg: #0a0e1a;
  --bg-2: #11172a;
  --card: #161b30;
  --fg: #e6e9f5;
  --muted: #8a91a8;
  --accent: #00e5ff;
  --accent-2: #7fffd4;
  --good: #4ade80;
  --warn: #facc15;
  --bad: #f87171;
}}
body {{
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ color: var(--accent-2); }}
.wrap {{ max-width: 1100px; margin: 0 auto; padding: 0 24px; }}
header.hero {{
  padding: 80px 0 60px;
  border-bottom: 1px solid var(--bg-2);
}}
.brand {{
  font-size: 14px; color: var(--accent); letter-spacing: 0.18em;
  text-transform: uppercase; margin-bottom: 16px;
}}
h1 {{
  font-size: clamp(40px, 6vw, 72px);
  line-height: 1.05;
  letter-spacing: -0.02em;
  font-weight: 800;
  margin-bottom: 20px;
}}
h1 em {{
  font-style: normal;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}}
.lead {{ color: var(--muted); font-size: 20px; max-width: 720px; }}
.live-pulse {{
  display: inline-flex; align-items: center; gap: 8px;
  padding: 6px 14px; background: rgba(74,222,128,0.12);
  border: 1px solid rgba(74,222,128,0.4); border-radius: 999px;
  font-size: 13px; color: var(--good); margin-bottom: 16px;
}}
.live-pulse::before {{
  content: ""; width: 8px; height: 8px; border-radius: 50%;
  background: var(--good); box-shadow: 0 0 8px var(--good);
  animation: pulse 1.6s infinite;
}}
@keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.4}} }}
.stats {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px; margin: 50px 0;
}}
.stat {{
  background: var(--card); padding: 22px; border-radius: 12px;
  border: 1px solid var(--bg-2);
}}
.stat-num {{
  font-size: 36px; font-weight: 700; color: var(--accent); margin-bottom: 4px;
}}
.stat-lbl {{ font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; }}
section {{ padding: 60px 0; border-bottom: 1px solid var(--bg-2); }}
h2 {{ font-size: 32px; margin-bottom: 32px; letter-spacing: -0.01em; }}
h2 small {{ color: var(--muted); font-size: 14px; font-weight: 400; margin-left: 12px; }}
.grid {{
  display: grid; gap: 18px;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
}}
.product {{
  background: var(--card); border: 1px solid var(--bg-2); border-radius: 14px;
  padding: 22px; transition: transform 120ms, border 120ms;
  display: flex; flex-direction: column; gap: 12px;
}}
.product:hover {{ transform: translateY(-2px); border-color: var(--accent); }}
.product-head {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
.product h3 {{ font-size: 18px; letter-spacing: -0.01em; }}
.product-tag {{
  display: inline-block; padding: 3px 10px; background: rgba(0,229,255,0.1);
  border: 1px solid rgba(0,229,255,0.3); border-radius: 999px;
  font-size: 11px; color: var(--accent); text-transform: uppercase; letter-spacing: 0.05em;
}}
.product-tagline {{ color: var(--muted); font-size: 14px; line-height: 1.5; }}
.product-meta {{ display: flex; gap: 14px; font-size: 12px; color: var(--muted); }}
.product-meta span {{ display: inline-flex; gap: 4px; }}
.product-meta strong {{ color: var(--fg); }}
.product-actions {{ display: flex; gap: 8px; margin-top: auto; }}
.product .open {{
  display: inline-block; padding: 8px 14px; border-radius: 8px;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  color: var(--bg); font-weight: 600; font-size: 13px;
}}
.product .open:hover {{ filter: brightness(1.1); color: var(--bg); }}
.product .score {{
  font-size: 12px; color: var(--muted);
  display: inline-flex; align-items: center; padding: 8px 12px;
  border: 1px solid var(--bg-2); border-radius: 8px;
}}
.feed {{
  background: var(--bg-2); border-radius: 12px; padding: 20px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px; max-height: 380px; overflow: auto;
}}
.feed-row {{ padding: 6px 0; color: var(--muted); border-bottom: 1px dashed rgba(255,255,255,0.05); }}
.feed-row:last-child {{ border: 0; }}
.feed-row .repo {{ color: var(--accent-2); margin-right: 10px; }}
.feed-row .msg {{ color: var(--fg); }}
footer {{
  padding: 40px 0 80px; color: var(--muted); font-size: 13px; text-align: center;
}}
footer a {{ margin: 0 6px; }}
.how {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 18px;
}}
.step {{
  background: var(--card); border: 1px solid var(--bg-2); border-radius: 12px;
  padding: 22px;
}}
.step-num {{ color: var(--accent); font-size: 14px; font-weight: 600; margin-bottom: 8px; }}
.step h4 {{ font-size: 16px; margin-bottom: 6px; }}
.step p {{ color: var(--muted); font-size: 14px; }}
</style>
</head>
<body>

<header class="hero">
  <div class="wrap">
    <span class="live-pulse">● LIVE — autonomous build cycle running</span>
    <div class="brand">axentx</div>
    <h1>An autonomous AI team that<br>ships <em>real SaaS products</em>, 24/7.</h1>
    <p class="lead">
      No human prompts. No manual deploys. {n_total} products in flight,
      {n_live} live right now. The AI harvests pain signals from the public web,
      validates demand, codes, tests, and ships — every minute of every hour.
    </p>
  </div>
</header>

<div class="wrap">
  <div class="stats">
    <div class="stat"><div class="stat-num" id="s-live" data-target="{n_live}">{n_live}</div><div class="stat-lbl">Live products</div></div>
    <div class="stat"><div class="stat-num" id="s-total" data-target="{n_total}">{n_total}</div><div class="stat-lbl">Total in pipeline</div></div>
    <div class="stat"><div class="stat-num" id="s-commits" data-target="{n_commits_24h}">{n_commits_24h}</div><div class="stat-lbl">Commits / 24h</div></div>
    <div class="stat"><div class="stat-num" id="s-pending" data-target="{n_pending}">{n_pending}</div><div class="stat-lbl">Features queued</div></div>
  </div>
</div>

<section>
  <div class="wrap">
    <h2>Live products <small>· ranked by demand signal</small></h2>
    <div class="grid">
      {product_cards}
    </div>
  </div>
</section>

<section style="background:linear-gradient(135deg,rgba(0,229,255,0.03),rgba(127,255,212,0.03));border-top:1px solid var(--bg-2);border-bottom:1px solid var(--bg-2)">
  <div class="wrap">
    <h2>Biz opportunities <small style="color:var(--muted);font-size:14px;font-weight:400;margin-left:12px">· {n_biz_plans} Thai-market briefs, AI-validated</small></h2>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-top:14px">
      <a href="https://axentx-biz.pages.dev/" style="background:var(--card);border:1px solid var(--bg-2);border-radius:12px;padding:20px;display:block;text-decoration:none;color:var(--fg);transition:transform 120ms,border-color 120ms" onmouseover="this.style.transform='translateY(-2px)';this.style.borderColor='var(--accent)'" onmouseout="this.style.transform='';this.style.borderColor='var(--bg-2)'">
        <div style="font-size:13px;color:var(--accent);letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">browse all</div>
        <h3 style="font-size:18px;margin-bottom:8px">{n_biz_plans} Thai-market opportunities</h3>
        <p style="color:var(--muted);font-size:14px;line-height:1.5">Asian trade arbitrage briefs · concrete TAM/GM/payback numbers · validated by 6-expert AI panel.</p>
        <p style="color:var(--accent-2);font-size:13px;margin-top:12px">axentx-biz.pages.dev →</p>
      </a>
    </div>
  </div>
</section>

<section>
  <div class="wrap">
    <h2>Live development feed <small>· last 20 commits across all repos</small></h2>
    <div class="feed">
      {feed_html}
    </div>
  </div>
</section>

<section>
  <div class="wrap">
    <h2>How it works</h2>
    <div class="how">
      <div class="step"><div class="step-num">1 · HARVEST</div><h4>Listen for pain</h4><p>20+ stream sources — Reddit, HN, IndieHackers, GitHub issues, RemoteOK, fund news, public job posts.</p></div>
      <div class="step"><div class="step-num">2 · VALIDATE</div><h4>Triple-gate</h4><p>Pain validator → market research (TAM/SAM/SOM) → blue-ocean + funding-evidence gate. ~70% rejected.</p></div>
      <div class="step"><div class="step-num">3 · SCOPE</div><h4>Pitch + extend</h4><p>Existing product gets a feature added (extend), or new SaaS gets scoped + designed by 8-model LLM panel.</p></div>
      <div class="step"><div class="step-num">4 · SHIP</div><h4>Code + commit</h4><p>Multi-VM dev fleet ships real code to GitHub on every cycle. Auto-rollback on test fail. Auto-deploys landing.</p></div>
    </div>
  </div>
</section>

<footer>
  <div class="wrap">
    Updated {ts} · {n_repos} repos · run by an AI · made by Ashira
    <br><br>
    <a href="https://github.com/arkashira">GitHub</a> ·
    <a href="mailto:hello@axentx.dev">Contact</a>
  </div>
</footer>

<script src="/live.js" defer></script>
</body>
</html>
"""


def _sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


def _http(method, url, headers=None, data=None, timeout=60):
    h = {"User-Agent": "axentx-index/1"}
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
    if s == 200:
        return True
    if s == 409 or "already exists" in b.lower() or "duplicate" in b.lower():
        return True
    log("index-deploy", f"  ✗ ensure project: {s} {b[:200]}")
    return False


def _deploy(token, html_text):
    if _blake3 is None:
        log("index-deploy", "  ✗ blake3 missing")
        return None
    content = html_text.encode()
    h = _blake3.blake3(content + b"html").hexdigest()[:32]
    manifest = {"/index.html": h}

    # JWT
    s, b = _http(
        "GET",
        f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
        f"/pages/projects/{PROJECT}/upload-token",
        headers={"Authorization": f"Bearer {token}"})
    d = json.loads(b)
    if not d.get("success"):
        log("index-deploy", f"  ✗ JWT: {s} {b[:200]}")
        return None
    jwt = d["result"]["jwt"]

    # check-missing
    s, b = _http(
        "POST",
        "https://api.cloudflare.com/client/v4/pages/assets/check-missing",
        headers={"Authorization": f"Bearer {jwt}"},
        data={"hashes": [h]})
    d = json.loads(b)
    missing = d.get("result") if d.get("success") else None
    if missing is None:
        log("index-deploy", f"  ✗ check-missing: {s} {b[:200]}")
        return None
    if missing:
        payload = [{
            "key": h,
            "value": base64.b64encode(content).decode(),
            "metadata": {"contentType": "text/html"},
            "base64": True,
        }]
        s, b = _http(
            "POST",
            "https://api.cloudflare.com/client/v4/pages/assets/upload",
            headers={"Authorization": f"Bearer {jwt}"}, data=payload, timeout=120)
        if not json.loads(b).get("success"):
            log("index-deploy", f"  ✗ upload: {s} {b[:200]}")
            return None

    # deployment
    boundary = f"----axentxIDX{int(time.time())}"
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
                 "Content-Type":
                 f"multipart/form-data; boundary={boundary}"},
        data=body, timeout=120)
    d = json.loads(b) if b else {}
    if d.get("success"):
        return d["result"].get("url")
    log("index-deploy", f"  ✗ deployment: {s} {b[:300]}")
    return None



def _deploy_multi(token, files):
    """files: {path: (content_bytes, content_type)}. Returns deployment URL."""
    if _blake3 is None:
        return None
    manifest = {}
    by_hash = {}
    for path, (content, ct) in files.items():
        ext = path.rsplit(".", 1)[-1] if "." in path else "bin"
        h = _blake3.blake3(content + ext.encode()).hexdigest()[:32]
        manifest[path] = h
        by_hash[h] = (content, ct)

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
        data={"hashes": list(by_hash.keys())})
    d = json.loads(b)
    missing = d.get("result") if d.get("success") else None
    if missing is None:
        return None
    if missing:
        payload = []
        for h in missing:
            content, ct = by_hash[h]
            payload.append({
                "key": h,
                "value": base64.b64encode(content).decode(),
                "metadata": {"contentType": ct},
                "base64": True,
            })
        s, b = _http(
            "POST",
            "https://api.cloudflare.com/client/v4/pages/assets/upload",
            headers={"Authorization": f"Bearer {jwt}"},
            data=payload, timeout=120)
        if not json.loads(b).get("success"):
            return None
    boundary = f"----axentxIDX{int(time.time())}"
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
                 "Content-Type":
                 f"multipart/form-data; boundary={boundary}"},
        data=body, timeout=120)
    d = json.loads(b) if b else {}
    if d.get("success"):
        return d["result"].get("url")
    return None


def _commits_last_24h():
    cmd = (
        "find /opt/axentx -maxdepth 3 -name '.git' -type d 2>/dev/null | "
        "while read g; do d=$(dirname $g); cd $d && "
        "git log --since='24 hours ago' --oneline 2>/dev/null | wc -l; done")
    out = _sh(cmd)
    return sum(int(x) for x in out.splitlines() if x.strip().isdigit())


def _all_repos_count():
    cmd = "find /opt/axentx -maxdepth 3 -name '.git' -type d 2>/dev/null | wc -l"
    out = _sh(cmd).strip()
    try:
        return int(out)
    except ValueError:
        return 0


def _recent_feed(n=20):
    """Get last N commits across all repos with repo:msg."""
    cmd = (
        "find /opt/axentx -maxdepth 3 -name '.git' -type d 2>/dev/null | "
        "while read g; do d=$(dirname $g); name=$(basename $d); "
        "cd $d && git log --since='6 hours ago' --pretty=format:\"%ai|$name|%s\" 2>/dev/null; "
        "done | sort -r")
    out = _sh(cmd)
    rows = []
    for line in out.splitlines()[:n]:
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        ts, repo, msg = parts
        # Strip prefixes
        msg = msg.replace("axentx-dev-bot: ", "").strip()
        if len(msg) > 110:
            msg = msg[:110] + "…"
        rows.append((ts[5:16], repo, msg))
    return rows


def render_index():
    """Build the index HTML from all state files (with biz plan count)."""
    top = {}
    if TOP.is_file():
        try:
            top = json.loads(TOP.read_text())
        except Exception:
            top = {}
    live_urls = {}
    if LIVE.is_file():
        try:
            live_urls = json.loads(LIVE.read_text())
        except Exception:
            live_urls = {}

    ranked = top.get("all_ranked", [])
    n_total = top.get("total_products", len(ranked))
    n_live = len(live_urls)
    n_pending = sum(p.get("signals", {}).get("pending_features", 0)
                    for p in ranked)
    n_commits_24h = _commits_last_24h()

    # Build cards: live products first (sorted by score), then non-live with score
    seen = set()
    cards = []
    for p in ranked:
        slug = p["slug"]
        seen.add(slug)
        live_url = (live_urls.get(slug) or {}).get("url")
        # Use production URL (without deployment hash prefix) for cleaner link
        if live_url:
            # Strip "https://<hash>.<project>.pages.dev" → "https://<project>.pages.dev"
            try:
                # Normalize
                from urllib.parse import urlparse
                u = urlparse(live_url)
                # If hostname has 4 parts and the first looks like a hash, strip it
                parts = u.hostname.split(".")
                if (len(parts) == 4 and parts[-2] == "pages"
                        and parts[-1] == "dev"
                        and len(parts[0]) in (8, 32)):
                    live_url = f"https://{'.'.join(parts[1:])}/"
                else:
                    live_url = live_url.rstrip("/") + "/"
            except Exception:
                pass

        score = p.get("score", 0)
        n_commits = p.get("signals", {}).get("commits_7d", 0)
        n_pending_p = p.get("signals", {}).get("pending_features", 0)
        category = (p.get("category") or "platform").replace("-", " ")
        if category == "uncategorized":
            category = "platform"
        tagline = (p.get("tagline") or "").strip()
        # cleanup HTML
        import re
        tagline = re.sub(r"<[^>]+>", "", tagline)
        tagline = re.sub(r"^[^\w]+", "", tagline)
        tagline = re.split(r"\s+·\s+", tagline)[0]
        if not tagline or len(tagline) < 10:
            tagline = f"AI-built {category} for teams that ship."
        tagline = tagline[:140]

        action = (f'<a class="open" href="{html.escape(live_url)}" '
                  f'target="_blank" rel="noopener">Open →</a>'
                  if live_url else
                  f'<span class="score">in pipeline</span>')

        cards.append(
            f'<div class="product">'
            f'<div class="product-head">'
            f'<h3>{html.escape(slug.replace("-", " ").title())}</h3>'
            f'<span class="product-tag">{html.escape(category)}</span>'
            f'</div>'
            f'<p class="product-tagline">{html.escape(tagline)}</p>'
            f'<div class="product-meta">'
            f'<span>★ <strong>{score:.0f}</strong></span>'
            f'<span>git <strong>{n_commits}</strong>/wk</span>'
            f'<span>queued <strong>{n_pending_p}</strong></span>'
            f'</div>'
            f'<div class="product-actions">{action}</div>'
            f'</div>')
        if len(cards) >= 18:
            break

    feed_rows = []
    for ts, repo, msg in _recent_feed(20):
        feed_rows.append(
            f'<div class="feed-row">'
            f'<span class="repo">{html.escape(repo)}</span>'
            f'<span class="msg">{html.escape(msg)}</span>'
            f'<span style="float:right;color:#555">{html.escape(ts)}</span>'
            f'</div>')
    feed_html = "\n".join(feed_rows) or "<div class='feed-row'>building…</div>"

    # # 2026-05-11 biz plans count for index card
    biz_plans_total = 0
    try:
        biz_dir = Path("/opt/axentx-biz")
        if biz_dir.is_dir():
            biz_plans_total = sum(1 for _ in biz_dir.glob("*/biz-plan.md"))
    except Exception:
        pass

    return HTML_TEMPLATE.format(
        n_live=n_live,
        n_total=n_total,
        n_commits_24h=n_commits_24h,
        n_pending=n_pending,
        n_biz_plans=biz_plans_total,
        n_repos=_all_repos_count(),
        product_cards="\n      ".join(cards),
        feed_html=feed_html,
        ts=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )




# 2026-05-11 cross-product RSS feed
def _all_repos_recent(n=50):
    """Cross-repo commit feed. Returns [(ts_iso, ts_rfc, repo, sha, msg)]."""
    cmd = (
        "find /opt/axentx -maxdepth 3 -name '.git' -type d 2>/dev/null | "
        "while read g; do d=$(dirname $g); name=$(basename $d); "
        "cd $d && git log --since='48 hours ago' "
        "--pretty=format:\"%aI|%aD|$name|%H|%s\" 2>/dev/null; "
        "done | sort -r | head -" + str(n))
    out = _sh(cmd, timeout=20)
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        iso, rfc, repo, sha, msg = parts
        import re as _re
        msg = _re.sub(r"^axentx-dev-bot:\s*", "", msg)[:140]
        rows.append((iso, rfc, repo, sha, msg))
    return rows


def render_feed():
    import xml.sax.saxutils as _sax
    import email.utils as _eu
    rows = _all_repos_recent(60)
    items = []
    for iso, rfc, repo, sha, msg in rows:
        link = f"https://github.com/arkashira/{repo}/commit/{sha}"
        items.append(
            f"<item>"
            f"<title>{_sax.escape(repo)}: {_sax.escape(msg)}</title>"
            f"<link>{link}</link>"
            f"<guid isPermaLink=\"true\">{link}</guid>"
            f"<pubDate>{rfc}</pubDate>"
            f"<category>{_sax.escape(repo)}</category>"
            f"<description>"
            f"Commit {sha[:7]} on {_sax.escape(repo)}: "
            f"{_sax.escape(msg)}</description>"
            f"</item>"
        )
    rss_now = _eu.format_datetime(
        datetime.datetime.now(datetime.timezone.utc))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        '<channel>\n'
        '<title>axentx · live development feed</title>\n'
        '<link>https://axentx.pages.dev/</link>\n'
        '<atom:link href="https://axentx.pages.dev/feed.xml" rel="self" '
        'type="application/rss+xml"/>\n'
        '<description>Last 60 commits across the entire axentx product '
        'portfolio — autonomous AI engineering, every commit, no human in '
        'the loop.</description>\n'
        '<language>en</language>\n'
        f'<lastBuildDate>{rss_now}</lastBuildDate>\n'
        + "\n".join(items)
        + "\n</channel></rss>\n"
    )


# 2026-05-11 multi-feed: JSON Feed + Atom + sitemap + robots

def render_jsonfeed():
    """JSON Feed 1.1 — https://www.jsonfeed.org/version/1.1/"""
    rows = _all_repos_recent(60)
    items = []
    for iso, _, repo, sha, msg in rows:
        link = f"https://github.com/arkashira/{repo}/commit/{sha}"
        items.append({
            "id": link,
            "url": link,
            "title": f"{repo}: {msg}",
            "content_text": f"Commit {sha[:7]} on {repo}: {msg}",
            "date_published": iso,
            "tags": [repo],
        })
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "axentx · live development feed",
        "home_page_url": "https://axentx.pages.dev/",
        "feed_url": "https://axentx.pages.dev/feed.json",
        "description":
            "Last 60 commits across the axentx product portfolio — "
            "autonomous AI engineering, every commit, no human in the loop.",
        "language": "en",
        "items": items,
    }
    return json.dumps(feed, indent=2, ensure_ascii=False)


def render_atom():
    """Atom 1.0 feed — RFC 4287."""
    import xml.sax.saxutils as _sax
    rows = _all_repos_recent(60)
    entries = []
    for iso, _, repo, sha, msg in rows:
        link = f"https://github.com/arkashira/{repo}/commit/{sha}"
        entries.append(
            f"<entry>"
            f"<title>{_sax.escape(repo)}: {_sax.escape(msg)}</title>"
            f'<link href="{link}"/>'
            f"<id>{link}</id>"
            f"<updated>{iso}</updated>"
            f"<category term=\"{_sax.escape(repo)}\"/>"
            f"<summary>Commit {sha[:7]} on {_sax.escape(repo)}: "
            f"{_sax.escape(msg)}</summary>"
            f"</entry>"
        )
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        '<title>axentx · live development feed</title>\n'
        '<link href="https://axentx.pages.dev/" rel="alternate"/>\n'
        '<link href="https://axentx.pages.dev/feed.atom" rel="self"/>\n'
        '<id>https://axentx.pages.dev/</id>\n'
        f'<updated>{now_iso}</updated>\n'
        '<subtitle>Last 60 commits across the axentx product portfolio</subtitle>\n'
        + "\n".join(entries) +
        "\n</feed>\n"
    )


def render_sitemap():
    """Global sitemap pointing at all live product URLs + /changelog."""
    try:
        live = json.loads(LIVE.read_text())
    except Exception:
        live = {}
    urls = ["https://axentx.pages.dev/"]
    for slug, info in live.items():
        url = info.get("url") or ""
        # Prod URL
        try:
            from urllib.parse import urlparse
            u = urlparse(url)
            parts = u.hostname.split(".")
            if (len(parts) == 4 and parts[-2] == "pages"
                    and parts[-1] == "dev" and len(parts[0]) in (8, 32)):
                prod = f"https://{'.'.join(parts[1:])}/"
            else:
                prod = url.rstrip("/") + "/"
        except Exception:
            prod = url
        urls.append(prod)
        urls.append(prod.rstrip("/") + "/changelog")
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    body = '<?xml version="1.0" encoding="UTF-8"?>\n'
    body += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for u in urls:
        body += (f"<url><loc>{u}</loc><lastmod>{now}</lastmod>"
                 f"<changefreq>hourly</changefreq></url>\n")
    body += "</urlset>\n"
    return body


def render_robots():
    return ("User-agent: *\n"
            "Allow: /\n"
            "Sitemap: https://axentx.pages.dev/sitemap.xml\n")


# 2026-05-11 API endpoints: /products.json + /stats.json

def render_products_api():
    """JSON catalog of all live products."""
    try:
        live = json.loads(LIVE.read_text())
    except Exception:
        live = {}
    try:
        top = json.loads(TOP.read_text())
    except Exception:
        top = {"all_ranked": []}

    products = []
    rank_lookup = {p["slug"]: p for p in top.get("all_ranked", [])}
    for slug, info in live.items():
        url = info.get("url", "")
        # Normalize to production URL
        try:
            from urllib.parse import urlparse
            u = urlparse(url)
            parts = u.hostname.split(".")
            if (len(parts) == 4 and parts[-2] == "pages"
                    and parts[-1] == "dev" and len(parts[0]) in (8, 32)):
                prod = "https://" + ".".join(parts[1:]) + "/"
            else:
                prod = url.rstrip("/") + "/"
        except Exception:
            prod = url

        r = rank_lookup.get(slug, {})
        import re as _re
        tag = (r.get("tagline") or "").strip()
        tag = _re.sub(r"<[^>]+>", "", tag)
        tag = _re.sub(r"^[^\w]+", "", tag)
        tag = _re.split(r"\s+·\s+", tag)[0][:160]

        products.append({
            "slug": slug,
            "url": prod,
            "category": r.get("category") or "platform",
            "score": r.get("score", 0),
            "tagline": tag,
            "signals": r.get("signals", {}),
            "deployed_at": info.get("deployed_at"),
            "rss": prod + "feed.xml",
            "changelog": prod + "changelog",
            "og_image": prod + "og.svg",
        })

    return json.dumps({
        "version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "count": len(products),
        "products": sorted(products, key=lambda p: -p["score"]),
    }, indent=2, ensure_ascii=False)


def render_stats_api():
    """Real-time pipeline metrics + # 2026-05-11 biz-section + resource alerts + badge svg resource alerts."""
    rows = _all_repos_recent(200)
    # Compute commits in last 24h, 1h, 7d
    import time as _t
    now = _t.time()
    h24 = h1 = d7 = 0
    by_repo_24h = {}
    for iso, _, repo, _, _ in rows:
        try:
            t = datetime.datetime.fromisoformat(iso).timestamp()
        except Exception:
            continue
        age = now - t
        if age < 3600:
            h1 += 1
        if age < 86400:
            h24 += 1
            by_repo_24h[repo] = by_repo_24h.get(repo, 0) + 1
        if age < 604800:
            d7 += 1

    try:
        top = json.loads(TOP.read_text())
        n_total = top.get("total_products", 0)
        n_pending = sum(p.get("signals", {}).get("pending_features", 0)
                        for p in top.get("all_ranked", []))
    except Exception:
        n_total = 0; n_pending = 0
    try:
        live = json.loads(LIVE.read_text())
        n_live = len(live)
    except Exception:
        n_live = 0

    # # 2026-05-11 resource metrics
    mem_used_mb = 0
    mem_total_mb = 1
    try:
        with open("/proc/meminfo") as f:
            mem = f.read()
        m1 = re.search(r"MemTotal:\s+(\d+)", mem)
        m2 = re.search(r"MemAvailable:\s+(\d+)", mem)
        if m1 and m2:
            mem_total_mb = int(m1.group(1)) // 1024
            mem_avail_mb = int(m2.group(1)) // 1024
            mem_used_mb = mem_total_mb - mem_avail_mb
    except Exception:
        pass
    load_1 = load_5 = load_15 = 0.0
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        load_1, load_5, load_15 = float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        pass
    mem_pct = round(mem_used_mb / max(mem_total_mb, 1) * 100, 1)
    alerts = []
    if mem_pct >= 88:
        alerts.append({"level": "warn", "type": "mem", "value": mem_pct, "msg": "mem usage >88%"})
    if load_1 > 8.0:
        alerts.append({"level": "warn", "type": "load", "value": load_1, "msg": "load >8.0"})

    # biz plans
    biz_plans_24h = 0; biz_plans_total = 0
    try:
        biz_dir = Path("/opt/axentx-biz")
        if biz_dir.is_dir():
            import time as _tm
            now = _tm.time()
            for p in biz_dir.glob("*/biz-plan.md"):
                biz_plans_total += 1
                try:
                    if (now - p.stat().st_mtime) < 86400:
                        biz_plans_24h += 1
                except Exception:
                    pass
    except Exception:
        pass

    return json.dumps({
        "version": 1,
        "generated_at":
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "products": {
            "live": n_live,
            "total_ranked": n_total,
            "features_in_queue": n_pending,
        },
        "commits": {
            "last_1h": h1,
            "last_24h": h24,
            "last_7d": d7,
            "by_repo_24h": dict(sorted(by_repo_24h.items(),
                                         key=lambda x: -x[1])),
        },
        "biz_plans": {
            "total": biz_plans_total,
            "last_24h": biz_plans_24h,
        },
        "resources": {
            "mem_used_mb": mem_used_mb,
            "mem_total_mb": mem_total_mb,
            "mem_pct": mem_pct,
            "load_1m": load_1,
            "load_5m": load_5,
            "load_15m": load_15,
        },
        "alerts": alerts,
    }, indent=2, ensure_ascii=False)



# 2026-05-11 dynamic shields-style badge for README embedding
def render_badge_svg(n_live, n_commits_24h):
    """SVG badge: "axentx · 14 LIVE · 200 commits/24h" — shields.io style."""
    left = "axentx"
    right = f"{n_live} LIVE · {n_commits_24h} commits/24h"
    # Width estimate: ~7px per char for narrow font, +14 padding each side
    left_w = len(left) * 7 + 14
    right_w = len(right) * 7 + 14
    total_w = left_w + right_w
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="20" '
        f'role="img" aria-label="axentx live status">\n'
        f'  <linearGradient id="b" x2="0" y2="100%">\n'
        f'    <stop offset="0" stop-color="#000" stop-opacity=".1"/>\n'
        f'    <stop offset="1" stop-opacity=".1"/>\n'
        f'  </linearGradient>\n'
        f'  <mask id="m"><rect width="{total_w}" height="20" rx="3" fill="#fff"/></mask>\n'
        f'  <g mask="url(#m)">\n'
        f'    <rect width="{left_w}" height="20" fill="#0a0e1a"/>\n'
        f'    <rect x="{left_w}" width="{right_w}" height="20" fill="#00e5ff"/>\n'
        f'    <rect width="{total_w}" height="20" fill="url(#b)"/>\n'
        f'  </g>\n'
        f'  <g fill="#fff" text-anchor="middle" font-family="Verdana,sans-serif" font-size="11">\n'
        f'    <text x="{left_w//2}" y="14" fill="#fff">{left}</text>\n'
        f'    <text x="{left_w + right_w//2}" y="14" fill="#0a0e1a" font-weight="700">{right}</text>\n'
        f'  </g>\n'
        f'</svg>'
    )

def main():
    log("index-deploy",
        f"start — cycle={CYCLE_SEC}s → axentx.pages.dev")
    if _blake3 is None:
        log("index-deploy", "✗ blake3 missing")
        return 1
    while not _stop:
        try:
            token = _cf_token()
            if not token:
                log("index-deploy", "✗ no CF token")
            else:
                if not _ensure_project(token):
                    log("index-deploy", "⚠ project ensure failed")
                else:
                    body = render_index()
                    feed_rss  = render_feed()
                    feed_atom = render_atom()
                    feed_json = render_jsonfeed()
                    sitemap   = render_sitemap()
                    robots    = render_robots()
                    products_api = render_products_api()
                    stats_api    = render_stats_api()
                    # # 2026-05-11 also build badge.svg
                    try:
                        _stats = json.loads(stats_api)
                        _n_live = _stats.get("products", {}).get("live", 0)
                        _n_commits = _stats.get("commits", {}).get("last_24h", 0)
                    except Exception:
                        _n_live = 0; _n_commits = 0
                    badge_svg = render_badge_svg(_n_live, _n_commits)
                    # # 2026-05-11 deploy /live.js
                    live_js_path = Path("/opt/surrogate-1-harvest/bin/axentx-live-widget.js")
                    live_js = live_js_path.read_text() if live_js_path.exists() else ""
                    out_dir = Path("/opt/axentx-live-index")
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / "index.html").write_text(body)
                    (out_dir / "feed.xml").write_text(feed_rss)
                    (out_dir / "feed.atom").write_text(feed_atom)
                    (out_dir / "feed.json").write_text(feed_json)
                    (out_dir / "sitemap.xml").write_text(sitemap)
                    (out_dir / "robots.txt").write_text(robots)
                    (out_dir / "products.json").write_text(products_api)
                    (out_dir / "stats.json").write_text(stats_api)
                    if live_js:
                        (out_dir / "live.js").write_text(live_js)
                    files_dict = {
                        "/index.html":    (body.encode(),         "text/html"),
                        "/feed.xml":      (feed_rss.encode(),     "application/rss+xml"),
                        "/feed.atom":     (feed_atom.encode(),    "application/atom+xml"),
                        "/feed.json":     (feed_json.encode(),    "application/feed+json"),
                        "/sitemap.xml":   (sitemap.encode(),      "application/xml"),
                        "/robots.txt":    (robots.encode(),       "text/plain"),
                        "/products.json": (products_api.encode(), "application/json"),
                        "/stats.json":    (stats_api.encode(),    "application/json"),
                        "/badge.svg":     (badge_svg.encode(),    "image/svg+xml"),
                    }
                    if live_js:
                        files_dict["/live.js"] = (live_js.encode(), "application/javascript")
                    url = _deploy_multi(token, files_dict)
                    if url:
                        log("index-deploy",
                            f"✓ index deployed → {url}  "
                            f"(prod: https://{PROJECT}.pages.dev/)")
                    else:
                        log("index-deploy", "⚠ deploy failed")
        except Exception as e:
            log("index-deploy",
                f"⚠ cycle: {type(e).__name__}: {str(e)[:160]}")
        for _ in range(CYCLE_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
