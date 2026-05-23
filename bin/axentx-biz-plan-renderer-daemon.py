#!/usr/bin/env python3
"""axentx Biz-Plan Renderer — turn /opt/axentx-biz/<slug>/biz-plan.md into LIVE
HTML landings at https://axentx-biz.pages.dev/<slug>/

Each plan = a long-form opportunity brief with:
  - Panel scores + verdict
  - Original source link
  - Multiple expert evaluations (Asian Trade · Thai Consumer · Retail Operator etc)
  - Concrete numbers (TAM, GM, payback, capital required)

Index at https://axentx-biz.pages.dev/ lists all plans with category + score.

Cycle: 30 min. Deploys via CF Pages Direct Upload (same pattern as
live-deployer / index-deployer).
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
from axentx_pipeline import log  # noqa: E402

try:
    import blake3 as _blake3
except ImportError:
    _blake3 = None

CYCLE_SEC = int(os.environ.get("BIZ_PLAN_CYCLE_SEC", "1800"))
BIZ_DIR = Path("/opt/axentx-biz")
OUT_DIR = Path("/opt/axentx-biz-live")
ACCT = "77fb5e6c3716be794dc3e8467ba9f285"
PROJECT = "axentx-biz"

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("biz-plan", "shutdown")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


PLAN_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} · axentx biz opportunity</title>
<meta name="description" content="Thai-market biz opportunity brief: {short_desc}">
<meta property="og:title" content="{title} · axentx biz opportunity">
<meta property="og:description" content="{short_desc}">
<meta property="og:type" content="article">
<meta property="og:url" content="https://axentx-biz.pages.dev/{slug}/">
<link rel="canonical" href="https://axentx-biz.pages.dev/{slug}/">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#0a0e1a;--bg-2:#11172a;--card:#161b30;--fg:#e6e9f5;--muted:#8a91a8;
  --accent:#00e5ff;--accent-2:#7fffd4;--good:#4ade80;--warn:#facc15;--bad:#f87171;
}}
body{{background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;line-height:1.65;-webkit-font-smoothing:antialiased}}
a{{color:var(--accent);text-decoration:none}}
a:hover{{color:var(--accent-2)}}
.wrap{{max-width:820px;margin:0 auto;padding:40px 24px}}
header{{padding:30px 0;border-bottom:1px solid var(--bg-2);margin-bottom:30px}}
.crumb{{font-size:13px;color:var(--accent);letter-spacing:0.14em;text-transform:uppercase;margin-bottom:10px}}
.crumb a{{color:var(--accent)}}
h1{{font-size:36px;letter-spacing:-0.01em;line-height:1.15;margin-bottom:14px;color:var(--fg)}}
.verdict-block{{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0 8px}}
.verdict{{padding:6px 14px;border-radius:999px;font-size:13px;font-weight:600;letter-spacing:0.04em}}
.verdict.go{{background:rgba(74,222,128,0.13);color:var(--good);border:1px solid rgba(74,222,128,0.3)}}
.verdict.iterate{{background:rgba(250,204,21,0.13);color:var(--warn);border:1px solid rgba(250,204,21,0.3)}}
.verdict.no-go{{background:rgba(248,113,113,0.13);color:var(--bad);border:1px solid rgba(248,113,113,0.3)}}
.stat{{padding:6px 14px;border-radius:999px;background:var(--bg-2);font-size:13px;color:var(--muted)}}
.stat strong{{color:var(--fg)}}
.source{{background:var(--card);border-left:3px solid var(--accent);padding:14px 18px;border-radius:8px;margin:24px 0;font-size:14px}}
.source-label{{color:var(--accent);font-size:12px;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px}}
section{{margin:30px 0}}
h2{{font-size:22px;margin-bottom:14px;color:var(--fg)}}
.panel{{background:var(--card);border:1px solid var(--bg-2);border-radius:10px;padding:18px 20px;margin-bottom:12px}}
.panel-head{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px}}
.panel-name{{font-weight:600;font-size:16px;color:var(--fg)}}
.panel-rationale{{color:var(--muted);font-size:14px;margin:8px 0 12px;line-height:1.55}}
.panel-meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px 16px;font-size:13px;color:var(--muted)}}
.panel-meta span strong{{color:var(--fg)}}
.cta-box{{background:linear-gradient(135deg, var(--card), var(--bg-2));border-radius:12px;padding:24px;margin:34px 0;text-align:center}}
.cta-box h3{{margin-bottom:8px}}
.cta-box p{{color:var(--muted);margin-bottom:16px}}
.cta-btn{{display:inline-block;padding:11px 22px;background:linear-gradient(90deg,var(--accent),var(--accent-2));color:var(--bg);font-weight:600;border-radius:8px;font-size:14px}}
.cta-btn:hover{{filter:brightness(1.1);color:var(--bg)}}
footer{{margin-top:50px;padding-top:24px;border-top:1px solid var(--bg-2);text-align:center;color:var(--muted);font-size:13px}}
footer a{{margin:0 6px}}
</style>
</head>
<body><div class="wrap">

<header>
  <div class="crumb"><a href="/">axentx · biz opportunities</a> ▸ {category}</div>
  <h1>{display_title}</h1>
  <div class="verdict-block">
    <span class="verdict {verdict_cls}">{verdict_text}</span>
    <span class="stat">Panel avg <strong>{panel_avg}</strong>/10</span>
    <span class="stat">Invest signal <strong>{invest_pct}%</strong></span>
    <span class="stat">Source <strong>{source_region}</strong></span>
  </div>
  {source_html}
</header>

<section>
  <h2>Expert panel evaluations</h2>
  {panels_html}
</section>

{numbers_html}

<section class="cta-box">
  <h3>Want to act on this?</h3>
  <p>Get the full brief + introductions to suppliers + market validation help.</p>
  <a class="cta-btn" href="mailto:hello@axentx.dev?subject=Biz brief: {slug}">Request full brief →</a>
</section>

<footer>
  Generated {ts} · plan id <code>{slug}</code>
  <br><br>
  <a href="/">all biz opportunities</a> ·
  <a href="https://axentx.pages.dev/">axentx products</a> ·
  <a href="https://axentx.pages.dev/feed.json">feed</a>
</footer>

</div></body></html>
"""


INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>axentx · {n_plans} Thai-market biz opportunities</title>
<meta name="description" content="{n_plans} validated biz opportunities for the Thai market — Asian trade arbitrage, blue-ocean plays, sourced via autonomous AI panel.">
<meta property="og:title" content="axentx · {n_plans} Thai-market biz opportunities">
<meta property="og:description" content="Validated by 6-expert AI panel · concrete TAM/GM/payback for each">
<meta property="og:type" content="website">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0a0e1a;--bg-2:#11172a;--card:#161b30;--fg:#e6e9f5;--muted:#8a91a8;--accent:#00e5ff;--accent-2:#7fffd4;--good:#4ade80;--warn:#facc15;--bad:#f87171}}
body{{background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.6}}
a{{color:var(--accent);text-decoration:none}}
.wrap{{max-width:1080px;margin:0 auto;padding:30px 24px}}
header{{padding:40px 0 30px;border-bottom:1px solid var(--bg-2);margin-bottom:30px}}
.brand{{font-size:13px;color:var(--accent);letter-spacing:0.18em;text-transform:uppercase;margin-bottom:10px}}
h1{{font-size:44px;letter-spacing:-0.02em;margin-bottom:12px}}
.lead{{color:var(--muted);font-size:17px;max-width:680px}}
.grid{{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin:20px 0}}
.plan{{background:var(--card);border:1px solid var(--bg-2);border-radius:12px;padding:20px;transition:transform 120ms,border 120ms;display:flex;flex-direction:column;gap:10px}}
.plan:hover{{transform:translateY(-2px);border-color:var(--accent)}}
.plan-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}}
.plan h3{{font-size:17px;letter-spacing:-0.01em;line-height:1.3;color:var(--fg)}}
.plan h3 a{{color:var(--fg)}}
.plan h3 a:hover{{color:var(--accent)}}
.plan-verdict{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;flex-shrink:0}}
.plan-verdict.go{{background:rgba(74,222,128,0.13);color:var(--good)}}
.plan-verdict.iterate{{background:rgba(250,204,21,0.13);color:var(--warn)}}
.plan-source{{color:var(--muted);font-size:13px}}
.plan-meta{{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--muted);margin-top:auto;padding-top:8px;border-top:1px dashed rgba(255,255,255,0.05)}}
.plan-meta strong{{color:var(--fg)}}
footer{{margin-top:60px;padding-top:30px;border-top:1px solid var(--bg-2);text-align:center;color:var(--muted);font-size:13px}}
footer a{{margin:0 6px;color:var(--accent)}}
</style>
</head>
<body><div class="wrap">

<header>
  <div class="brand">axentx · biz opportunities</div>
  <h1>{n_plans} Thai-market opportunities,<br>scored & ranked by AI</h1>
  <p class="lead">Every plan validated by a 6-expert AI panel (Asian Trade, Thai Consumer, Retail Operator, Finance, Logistics, Regulatory). Concrete numbers: Thai TAM · GM · payback · min capital.</p>
</header>

<div class="grid">
  {plans_html}
</div>

<footer>
  Updated {ts} · generated autonomously · part of axentx
  <br><br>
  <a href="https://axentx.pages.dev/">axentx products</a> ·
  <a href="https://axentx-status.pages.dev/">status</a> ·
  <a href="https://axentx.pages.dev/feed.json">json feed</a>
</footer>

</div></body></html>
"""


def _http(method, url, headers=None, data=None, timeout=60):
    h = {"User-Agent": "axentx-biz-plan/1"}
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
    log("biz-plan", f"  ⚠ ensure project: {s} {b[:200]}")
    return False


def _deploy_multi(token, files):
    """{path: (content_bytes, content_type)} → URL or None."""
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

    # Chunk the missing check (CF limits to ~1000 hashes per req)
    hashes = list(by_hash.keys())
    missing = []
    for i in range(0, len(hashes), 100):
        chunk = hashes[i:i + 100]
        s, b = _http(
            "POST",
            "https://api.cloudflare.com/client/v4/pages/assets/check-missing",
            headers={"Authorization": f"Bearer {jwt}"},
            data={"hashes": chunk})
        d = json.loads(b)
        if d.get("success"):
            missing.extend(d.get("result") or [])

    if missing:
        # Upload in chunks of 50
        for i in range(0, len(missing), 50):
            chunk = missing[i:i + 50]
            payload = []
            for h in chunk:
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
                log("biz-plan", f"  ⚠ upload chunk: {s} {b[:200]}")
                return None

    boundary = f"----axentxBIZ{int(time.time())}"
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
    log("biz-plan", f"  ⚠ deployment: {s} {b[:300]}")
    return None


# ── Markdown parser (lightweight) ───────────────────────────────────────

def parse_plan_md(path):
    """Parse a biz-plan.md file. Returns dict with verdict, panels, source, etc."""
    text = path.read_text(encoding="utf-8")
    out = {
        "slug": path.parent.name,
        "raw_title": "",
        "verdict": "GO",
        "panel_avg": 0.0,
        "go_pct": 0.0,
        "nogo_pct": 0.0,
        "invest_pct": 0.0,
        "source_text": "",
        "source_url": "",
        "source_region": "",
        "source_title": "",
        "panels": [],
        "ts": "",
    }

    # Title
    m = re.search(r"^# (.+?)$", text, re.MULTILINE)
    if m:
        out["raw_title"] = m.group(1).strip()

    # Verdict + scores
    m = re.search(r"## Pitch verdict:\s*(\S+)", text)
    if m:
        out["verdict"] = m.group(1).strip()
    m = re.search(r"Weighted avg:\s*([\d.]+)", text)
    if m:
        out["panel_avg"] = float(m.group(1))
    m = re.search(r"GO weight:\s*([\d.]+)%\s*/\s*NO-GO:\s*([\d.]+)%", text)
    if m:
        out["go_pct"] = float(m.group(1))
        out["nogo_pct"] = float(m.group(2))
    m = re.search(r"Invest signal:\s*([\d.]+)%", text)
    if m:
        out["invest_pct"] = float(m.group(1))

    # Source block
    m = re.search(r"## Original pain/opportunity\s*\n```\s*\n(.+?)\n```",
                  text, re.DOTALL)
    if m:
        source_block = m.group(1)
        out["source_text"] = source_block
        sm = re.search(r"\*\*Title:\*\*\s*(.+)", source_block)
        if sm:
            out["source_title"] = sm.group(1).strip()
        sm = re.search(r"\*\*Source:\*\*\s*(\S+)", source_block)
        if sm:
            out["source_url"] = sm.group(1).strip()
        sm = re.search(r"\*\*Region:\*\*\s*(.+)", source_block)
        if sm:
            out["source_region"] = sm.group(1).strip()

    # Panels
    for panel_match in re.finditer(
        r"### (.+?):\s*(\S+)\s*\(score=(\d+)\)\s*\n"
        r"((?:- .+\n)+)",
        text,
    ):
        name = panel_match.group(1).strip()
        verdict = panel_match.group(2).strip()
        score = int(panel_match.group(3))
        body = panel_match.group(4)
        # Extract bullet fields
        fields = {}
        for line in body.splitlines():
            lm = re.match(r"- ([^:]+):\s*(.+)", line)
            if lm:
                fields[lm.group(1).strip().lower()] = lm.group(2).strip()
        out["panels"].append({
            "name": name,
            "verdict": verdict,
            "score": score,
            "fields": fields,
        })

    return out


def derive_display_title(plan):
    """Pick a human-friendly title for the plan."""
    if plan["source_title"]:
        return plan["source_title"]
    # Else derive from slug
    return plan["slug"].replace("-", " ").title()


def derive_category(plan):
    """Categorize plan by region/source."""
    slug = plan["slug"].lower()
    region = (plan["source_region"] or "").upper()
    if "japan" in slug or "JAPAN" in region:
        return "Japan → Thailand"
    if "china" in slug or "CHINA" in region:
        return "China → Thailand"
    if "sea" in slug or "asean" in slug:
        return "SEA"
    if "asia" in slug:
        return "Pan-Asia"
    if "premium" in slug:
        return "Premium / luxury"
    if "heuristic" in slug:
        return "Heuristic-tagged"
    if "score" in slug:
        return "Scored opportunity"
    return "Thai market"


def render_plan_html(plan):
    title = derive_display_title(plan)
    category = derive_category(plan)
    verdict_cls = "go" if plan["verdict"] == "GO" else (
        "iterate" if plan["verdict"] in ("ITERATE", "PIVOT") else "no-go")
    short_desc = (plan["source_title"] or title)[:160]

    # Source block HTML
    source_html = ""
    if plan["source_url"] or plan["source_title"]:
        source_html = (
            f'<div class="source">'
            f'<div class="source-label">Original signal</div>'
            f'{html.escape(plan["source_title"])}'
        )
        if plan["source_url"]:
            source_html += (
                f'<div style="margin-top:6px"><a href="{html.escape(plan["source_url"])}" '
                f'target="_blank" rel="noopener">{html.escape(plan["source_url"])}</a></div>'
            )
        source_html += "</div>"

    # Panels HTML
    panels_html = []
    for p in plan["panels"]:
        rationale = p["fields"].get("rationale", "")
        # Build meta grid
        meta_items = []
        for k in ("thai tam", "demand", "channel", "source", "supplier",
                  "landed cost", "gm", "payback", "min capital"):
            if k in p["fields"]:
                label = k.upper().replace("THAI ", "")
                meta_items.append(
                    f'<span><strong>{html.escape(label)}:</strong> '
                    f'{html.escape(p["fields"][k])}</span>')
        meta_html = ('<div class="panel-meta">' + "".join(meta_items)
                     + "</div>") if meta_items else ""
        pv_cls = "good" if p["verdict"] == "GO" else (
            "warn" if p["verdict"] in ("PIVOT", "ITERATE") else "bad")
        pv_color = {
            "good": "#4ade80", "warn": "#facc15", "bad": "#f87171",
        }[pv_cls]
        panels_html.append(
            f'<div class="panel">'
            f'<div class="panel-head">'
            f'<span class="panel-name">{html.escape(p["name"])}</span>'
            f'<span style="color:{pv_color};font-weight:600;font-size:13px;'
            f'letter-spacing:0.05em">{html.escape(p["verdict"])} · '
            f'{p["score"]}/10</span>'
            f'</div>'
            f'<div class="panel-rationale">{html.escape(rationale)}</div>'
            f'{meta_html}'
            f'</div>'
        )

    # Aggregate numbers block (sum/avg from panels)
    numbers_html = ""
    if plan["panels"]:
        # Try extract TAM range, GM range
        tams = []
        gms = []
        paybacks = []
        capitals = []
        for p in plan["panels"]:
            f = p["fields"]
            t = f.get("thai tam", "")
            tm = re.search(r"(\d+)\s*M", t)
            if tm:
                tams.append(int(tm.group(1)))
            gm = re.search(r"(\d+)%", f.get("gm", ""))
            if gm:
                gms.append(int(gm.group(1)))
            pb = re.search(r"(\d+)\s*mo", f.get("payback", ""))
            if pb:
                paybacks.append(int(pb.group(1)))
            cap = re.search(r"([\d.]+)\s*M", f.get("min capital", ""))
            if cap:
                capitals.append(float(cap.group(1)))
        if tams or gms:
            stats = []
            if tams:
                stats.append(f'<span><strong>{min(tams)}–{max(tams)}M THB</strong> Thai TAM range</span>')
            if gms:
                stats.append(f'<span><strong>{min(gms)}–{max(gms)}%</strong> gross margin</span>')
            if paybacks:
                stats.append(f'<span><strong>{min(paybacks)}–{max(paybacks)} mo</strong> payback</span>')
            if capitals:
                stats.append(f'<span><strong>฿{min(capitals):.1f}–{max(capitals):.1f}M</strong> min capital</span>')
            numbers_html = (
                '<section><h2>Key numbers</h2>'
                '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));'
                'gap:12px 16px;font-size:14px;color:var(--muted);background:var(--card);'
                'padding:18px;border-radius:10px">'
                + "".join(stats) +
                '</div></section>'
            )

    return PLAN_TEMPLATE.format(
        slug=html.escape(plan["slug"]),
        title=html.escape(title)[:80],
        display_title=html.escape(title),
        short_desc=html.escape(short_desc),
        category=html.escape(category),
        verdict_cls=verdict_cls,
        verdict_text=html.escape(plan["verdict"]),
        panel_avg=f'{plan["panel_avg"]:.1f}',
        invest_pct=f'{plan["invest_pct"]:.0f}',
        source_region=html.escape(plan["source_region"] or "Asia"),
        source_html=source_html,
        panels_html="\n".join(panels_html),
        numbers_html=numbers_html,
        ts=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )


def render_index_html(plans):
    """Render the index page listing all plans."""
    plans_sorted = sorted(plans, key=lambda p: -p["panel_avg"])
    cards = []
    for p in plans_sorted:
        title = derive_display_title(p)
        category = derive_category(p)
        verdict_cls = "go" if p["verdict"] == "GO" else "iterate"
        # Try to grab a meta line from first panel
        meta_bits = []
        if p["panels"]:
            f0 = p["panels"][0]["fields"]
            if "thai tam" in f0:
                meta_bits.append(f'<strong>{html.escape(f0["thai tam"])}</strong> TAM')
            if "gm" in f0:
                meta_bits.append(f'<strong>{html.escape(f0["gm"])}</strong> GM')
            if "payback" in f0:
                meta_bits.append(f'<strong>{html.escape(f0["payback"])}</strong> payback')
        meta_html = " · ".join(meta_bits)
        cards.append(
            f'<div class="plan">'
            f'<div class="plan-head">'
            f'<h3><a href="/{html.escape(p["slug"])}/">{html.escape(title[:100])}</a></h3>'
            f'<span class="plan-verdict {verdict_cls}">{html.escape(p["verdict"])}</span>'
            f'</div>'
            f'<div class="plan-source">{html.escape(category)} · '
            f'panel avg <strong style="color:var(--fg)">{p["panel_avg"]:.1f}</strong>/10 · '
            f'invest <strong style="color:var(--fg)">{p["invest_pct"]:.0f}%</strong></div>'
            f'<div class="plan-meta">{meta_html}</div>'
            f'</div>'
        )
    return INDEX_TEMPLATE.format(
        n_plans=len(plans),
        plans_html="\n".join(cards),
        ts=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )


def main():
    log("biz-plan",
        f"start — cycle={CYCLE_SEC}s → axentx-biz.pages.dev")
    if _blake3 is None:
        log("biz-plan", "✗ blake3 missing")
        return 1
    while not _stop:
        try:
            # Find all plans
            plan_files = list(BIZ_DIR.glob("*/biz-plan.md"))
            log("biz-plan", f"▸ found {len(plan_files)} biz plans")
            plans = []
            for path in plan_files:
                try:
                    p = parse_plan_md(path)
                    if p["panels"]:  # require at least 1 panel
                        plans.append(p)
                except Exception as e:
                    log("biz-plan",
                        f"  ⚠ parse {path.parent.name}: {type(e).__name__}")
                    continue

            # Render + write
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            files = {}
            for p in plans:
                try:
                    body = render_plan_html(p)
                    out_dir = OUT_DIR / p["slug"]
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / "index.html").write_text(body, encoding="utf-8")
                    files[f'/{p["slug"]}/index.html'] = (
                        body.encode(), "text/html")
                except Exception as e:
                    log("biz-plan",
                        f"  ⚠ render {p['slug']}: {type(e).__name__}: {str(e)[:60]}")

            # Index
            try:
                index_html = render_index_html(plans)
                (OUT_DIR / "index.html").write_text(index_html, encoding="utf-8")
                files["/index.html"] = (index_html.encode(), "text/html")
            except Exception as e:
                log("biz-plan",
                    f"  ⚠ render index: {type(e).__name__}: {str(e)[:60]}")

            # Deploy
            token = _cf_token()
            if token and _ensure_project(token) and files:
                url = _deploy_multi(token, files)
                if url:
                    log("biz-plan",
                        f"✓ deployed {len(plans)} biz plans → "
                        f"{url} (prod: https://{PROJECT}.pages.dev/)")
                else:
                    log("biz-plan", "⚠ deploy returned no URL")
            else:
                log("biz-plan",
                    f"⊘ skip deploy (token={bool(token)}, files={len(files)})")
        except Exception as e:
            log("biz-plan",
                f"⚠ cycle: {type(e).__name__}: {str(e)[:160]}")
        for _ in range(CYCLE_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
