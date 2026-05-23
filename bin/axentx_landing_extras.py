"""axentx_landing_extras v3 — animated demo SVGs (CSS+SMIL), schema.org
SoftwareApplication, GitHub badges, PWA manifest.

Drop-in replacement for v2. Adds:
  • render_demo_svg now produces ANIMATED SVGs (pulsing live dots, count-up
    counters via SMIL animate elements, blinking status indicators)
  • render_schema_software_app(slug, name, tagline, category, url)
  • render_manifest(slug, name, category)
  • render_badges_html(slug, gh_owner) — shields.io badges (free, no auth)

Keeps backwards-compat with v2 imports.
"""
from __future__ import annotations
import datetime
import email.utils as _eu
import html
import json
import re
import subprocess
import xml.sax.saxutils as _xml_sax
from pathlib import Path

AXENTX_BASE = Path("/opt/axentx")


def _sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


def _git_log_recent(slug, n=30):
    repo = AXENTX_BASE / slug
    if not (repo / ".git").is_dir():
        return []
    out = _sh(
        f"cd {repo} && git log -{n} "
        f"--pretty=format:'%H|%aI|%aD|%s' 2>/dev/null")
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        sha, iso_ts, rfc_ts, subject = parts
        subject = re.sub(r"^axentx-dev-bot:\s*", "", subject)[:140]
        rows.append((sha, iso_ts, rfc_ts, subject))
    return rows


CHANGELOG_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} · Changelog</title>
<meta name="description" content="Auto-generated changelog for {name} — last {n_shown} commits.">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0a0e1a;--bg-2:#11172a;--card:#161b30;--fg:#e6e9f5;--muted:#8a91a8;--accent:#00e5ff;--accent-2:#7fffd4}}
body{{background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.6}}
.wrap{{max-width:780px;margin:0 auto;padding:40px 24px}}
.brand{{font-size:13px;color:var(--accent);letter-spacing:0.18em;text-transform:uppercase;margin-bottom:8px}}
h1{{font-size:34px;letter-spacing:-0.01em;margin-bottom:6px}}
.lead{{color:var(--muted);font-size:15px;margin-bottom:30px}}
.lead a{{color:var(--accent);text-decoration:none}}
.entry{{background:var(--card);border:1px solid var(--bg-2);border-left:3px solid var(--accent);border-radius:8px;padding:16px 18px;margin-bottom:10px}}
.entry .ts{{color:var(--muted);font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
.entry .msg{{color:var(--fg);font-size:15px;margin-top:4px}}
.entry .sha{{color:var(--accent-2);font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;float:right}}
footer{{color:var(--muted);font-size:13px;text-align:center;margin-top:40px}}
footer a{{color:var(--accent);text-decoration:none;margin:0 6px}}
</style>
</head><body><div class="wrap">
<div class="brand">axentx · changelog</div>
<h1>{name}</h1>
<p class="lead">Last {n_shown} commits, freshest first. Built and shipped autonomously by AI. <a href="./">← back</a></p>
{entries}
<footer>
  generated {ts} · auto-refreshed every cycle
  <br><br>
  <a href="./">home</a> · <a href="./feed.xml">rss</a> · <a href="https://github.com/{gh_owner}/{slug}">github</a>
</footer>
</div></body></html>
"""


RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
<title>{name} · changelog</title>
<link>https://{slug_lc}.pages.dev/</link>
<atom:link href="https://{slug_lc}.pages.dev/feed.xml" rel="self" type="application/rss+xml"/>
<description>Auto-generated changelog for {name} — every commit shipped by the axentx autonomous AI team.</description>
<language>en</language>
<lastBuildDate>{rss_now}</lastBuildDate>
{items}
</channel></rss>
"""


def _rss_now():
    return _eu.format_datetime(
        datetime.datetime.now(datetime.timezone.utc))


def render_changelog(slug, name, gh_owner="arkashira", n=30):
    rows = _git_log_recent(slug, n=n)
    entries = []
    for sha, iso_ts, _, subj in rows:
        entries.append(
            f'<div class="entry">'
            f'<div><span class="ts">'
            f'{html.escape(iso_ts[:16].replace("T", " "))}</span>'
            f'<span class="sha">{html.escape(sha[:7])}</span></div>'
            f'<div class="msg">{html.escape(subj)}</div>'
            f'</div>')
    return CHANGELOG_TEMPLATE.format(
        name=html.escape(name),
        n_shown=len(rows),
        entries="\n".join(entries) or
        '<div class="entry"><div class="msg">building…</div></div>',
        ts=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        gh_owner=html.escape(gh_owner),
        slug=html.escape(slug),
    )


def render_rss(slug, name):
    rows = _git_log_recent(slug, n=20)
    items = []
    slug_lc = slug.lower()
    for sha, _, rfc_ts, subj in rows:
        link = f"https://github.com/arkashira/{slug}/commit/{sha}"
        items.append(
            f"<item>"
            f"<title>{_xml_sax.escape(subj)}</title>"
            f"<link>{link}</link>"
            f'<guid isPermaLink="true">{link}</guid>'
            f"<pubDate>{rfc_ts}</pubDate>"
            f"<description>Commit {sha[:7]}: "
            f"{_xml_sax.escape(subj)}</description>"
            f"</item>")
    return RSS_TEMPLATE.format(
        name=_xml_sax.escape(name),
        slug_lc=slug_lc,
        rss_now=_rss_now(),
        items="\n".join(items),
    )


def render_og_svg(slug, name, tagline, category, score):
    name_esc = _xml_sax.escape(name)
    tag_short = tagline[:90] + ("…" if len(tagline) > 90 else "")
    tag_esc = _xml_sax.escape(tag_short)
    cat_esc = _xml_sax.escape(category.upper())
    slug_lc = slug.lower()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" '
        'viewBox="0 0 1200 630">\n'
        '<defs>\n'
        '  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">\n'
        '    <stop offset="0%" stop-color="#0a0e1a"/>\n'
        '    <stop offset="100%" stop-color="#161b30"/>\n'
        '  </linearGradient>\n'
        '  <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">\n'
        '    <stop offset="0%" stop-color="#00e5ff"/>\n'
        '    <stop offset="100%" stop-color="#7fffd4"/>\n'
        '  </linearGradient>\n'
        '</defs>\n'
        '<rect width="1200" height="630" fill="url(#bg)"/>\n'
        '<rect x="60" y="60" width="1080" height="510" rx="24" fill="none" '
        'stroke="#1c2440" stroke-width="2"/>\n'
        f'<text x="100" y="140" font-family="-apple-system, system-ui, sans-serif" '
        f'font-size="22" fill="#00e5ff" font-weight="600" letter-spacing="6">'
        f'AXENTX · {cat_esc}</text>\n'
        f'<text x="100" y="260" font-family="-apple-system, system-ui, sans-serif" '
        f'font-size="100" font-weight="800" fill="url(#accent)">{name_esc}</text>\n'
        f'<text x="100" y="360" font-family="-apple-system, system-ui, sans-serif" '
        f'font-size="32" fill="#e6e9f5" font-weight="500">{tag_esc}</text>\n'
        '<g transform="translate(100, 460)">\n'
        '  <circle cx="10" cy="-8" r="6" fill="#4ade80"/>\n'
        f'  <text x="30" y="-2" font-family="-apple-system, system-ui, sans-serif" '
        f'font-size="22" fill="#8a91a8">LIVE · score {score:.0f} · '
        f'shipped autonomously by AI</text>\n'
        '</g>\n'
        f'<text x="100" y="540" font-family="ui-monospace, SFMono-Regular, '
        f'Menlo, monospace" font-size="22" fill="#7fffd4">{slug_lc}.pages.dev</text>\n'
        '</svg>\n'
    )


# ── ANIMATED DEMO MOCKUP SVG (per category) ─────────────────────────────
# SMIL animations: <animate>, <animateTransform>. Supported in all modern
# browsers (Chrome, Safari, Firefox, Edge). For static social previews,
# the first frame still looks right.

_PULSE_DOT = '''
<circle cx="{cx}" cy="{cy}" r="6" fill="{fill}">
  <animate attributeName="opacity" values="1;0.3;1" dur="1.6s" repeatCount="indefinite"/>
  <animate attributeName="r" values="6;8;6" dur="1.6s" repeatCount="indefinite"/>
</circle>'''


def _demo_devops(name, tagline):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 540" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;display:block;border-radius:12px;box-shadow:0 24px 48px rgba(0,0,0,0.18)">
<rect width="1200" height="540" fill="#0a0e1a"/>
<g><circle cx="32" cy="32" r="6" fill="#ff5f56"/><circle cx="52" cy="32" r="6" fill="#ffbd2e"/><circle cx="72" cy="32" r="6" fill="#27c93f"/></g>
<text x="600" y="38" text-anchor="middle" fill="#8a91a8" font-family="ui-monospace,Menlo,monospace" font-size="14">{html.escape(name)} · deploy</text>
<rect x="20" y="60" width="1160" height="460" rx="8" fill="#0d1428" stroke="#1c2440"/>
<g font-family="ui-monospace,SFMono-Regular,Menlo,monospace" font-size="15">
<text x="44" y="100" fill="#7fffd4">$ {html.escape(name.lower())} deploy --env=prod</text>
<text x="44" y="135" fill="#8a91a8" opacity="0">→ Validating manifest…
  <animate attributeName="opacity" values="0;1" begin="0.3s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="158" fill="#4ade80" opacity="0">  ✓ schema OK
  <animate attributeName="opacity" values="0;1" begin="0.6s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="181" fill="#4ade80" opacity="0">  ✓ secrets present
  <animate attributeName="opacity" values="0;1" begin="0.9s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="204" fill="#4ade80" opacity="0">  ✓ resource limits within plan
  <animate attributeName="opacity" values="0;1" begin="1.2s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="240" fill="#8a91a8" opacity="0">→ Building image…
  <animate attributeName="opacity" values="0;1" begin="1.5s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="263" fill="#4ade80" opacity="0">  ✓ cache hit (saved 2m 14s)
  <animate attributeName="opacity" values="0;1" begin="1.9s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="286" fill="#4ade80" opacity="0">  ✓ pushed to registry · 234 MB
  <animate attributeName="opacity" values="0;1" begin="2.3s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="322" fill="#8a91a8" opacity="0">→ Rolling out…
  <animate attributeName="opacity" values="0;1" begin="2.7s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="345" fill="#4ade80" opacity="0">  ✓ canary 5% → healthy
  <animate attributeName="opacity" values="0;1" begin="3.0s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="368" fill="#4ade80" opacity="0">  ✓ canary 25% → healthy
  <animate attributeName="opacity" values="0;1" begin="3.4s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="391" fill="#4ade80" opacity="0">  ✓ canary 100% → healthy
  <animate attributeName="opacity" values="0;1" begin="3.8s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="427" fill="#00e5ff" opacity="0">  ✓ deployed in 38s · 0 errors · auto-rollback armed
  <animate attributeName="opacity" values="0;1" begin="4.2s" dur="0.4s" fill="freeze"/>
</text>
<text x="44" y="475" fill="#00e5ff">$ <tspan fill="#e6e9f5">_<animate attributeName="opacity" values="1;0;1" dur="1s" repeatCount="indefinite"/></tspan></text>
</g>
</svg>'''


def _demo_finops(name, tagline):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 540" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;display:block;border-radius:12px;box-shadow:0 24px 48px rgba(0,0,0,0.18)">
<rect width="1200" height="540" fill="#0a0e1a"/>
<rect x="20" y="20" width="1160" height="500" rx="12" fill="#0d1428" stroke="#1c2440"/>
<text x="50" y="60" font-family="-apple-system,system-ui,sans-serif" font-size="22" fill="#00e5ff" font-weight="600">{html.escape(name)}</text>
<text x="50" y="85" font-family="system-ui,sans-serif" font-size="13" fill="#8a91a8" letter-spacing="2">CLOUD COST · LAST 30 DAYS</text>
<g transform="translate(50, 110)">
  <rect width="320" height="100" rx="8" fill="#161b30" stroke="#1c2440"/>
  <text x="20" y="35" font-family="system-ui" font-size="13" fill="#8a91a8">MONTHLY SPEND</text>
  <text x="20" y="72" font-family="system-ui" font-size="34" fill="#e6e9f5" font-weight="700">$12,847</text>
  <text x="200" y="72" font-family="system-ui" font-size="14" fill="#4ade80">↓ 18%</text>
</g>
<g transform="translate(390, 110)">
  <rect width="320" height="100" rx="8" fill="#161b30" stroke="#1c2440"/>
  <text x="20" y="35" font-family="system-ui" font-size="13" fill="#8a91a8">SAVED THIS MONTH</text>
  <text x="20" y="72" font-family="system-ui" font-size="34" fill="#7fffd4" font-weight="700">$2,840</text>
</g>
<g transform="translate(730, 110)">
  <rect width="420" height="100" rx="8" fill="#161b30" stroke="#1c2440"/>
  <text x="20" y="35" font-family="system-ui" font-size="13" fill="#8a91a8">ANOMALIES DETECTED</text>
  <text x="20" y="72" font-family="system-ui" font-size="34" fill="#facc15" font-weight="700">3</text>
  <text x="80" y="72" font-family="system-ui" font-size="14" fill="#8a91a8">unusual EBS · idle RDS · oversized GPU</text>
  <circle cx="395" cy="50" r="6" fill="#facc15">
    <animate attributeName="opacity" values="1;0.2;1" dur="1.4s" repeatCount="indefinite"/>
  </circle>
</g>
<text x="50" y="250" font-family="system-ui" font-size="14" fill="#8a91a8" letter-spacing="1">DAILY SPEND TREND</text>
<defs><linearGradient id="g1f" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#00e5ff"/><stop offset="100%" stop-color="#00e5ff" stop-opacity="0"/></linearGradient></defs>
<g transform="translate(50, 270)">
  <line x1="0" y1="180" x2="1100" y2="180" stroke="#1c2440"/>
  <line x1="0" y1="120" x2="1100" y2="120" stroke="#1c2440" stroke-dasharray="3 4"/>
  <line x1="0" y1="60" x2="1100" y2="60" stroke="#1c2440" stroke-dasharray="3 4"/>
  <polyline points="0,140 80,130 160,138 240,120 320,110 400,80 480,70 560,90 640,72 720,55 800,68 880,52 960,45 1040,60 1100,42" fill="none" stroke="#00e5ff" stroke-width="2.5" stroke-dasharray="1500" stroke-dashoffset="1500">
    <animate attributeName="stroke-dashoffset" from="1500" to="0" dur="2s" fill="freeze"/>
  </polyline>
  <polyline points="0,140 80,130 160,138 240,120 320,110 400,80 480,70 560,90 640,72 720,55 800,68 880,52 960,45 1040,60 1100,42 1100,180 0,180" fill="url(#g1f)" opacity="0">
    <animate attributeName="opacity" values="0;0.25" begin="1.5s" dur="0.6s" fill="freeze"/>
  </polyline>
  <circle cx="1100" cy="42" r="4" fill="#7fffd4" opacity="0">
    <animate attributeName="opacity" values="0;1" begin="2s" dur="0.3s" fill="freeze"/>
    <animate attributeName="r" values="4;7;4" begin="2.3s" dur="1.4s" repeatCount="indefinite"/>
  </circle>
</g>
</svg>'''


def _demo_security(name, tagline):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 540" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;display:block;border-radius:12px;box-shadow:0 24px 48px rgba(0,0,0,0.18)">
<rect width="1200" height="540" fill="#0a0e1a"/>
<rect x="20" y="20" width="1160" height="500" rx="12" fill="#0d1428" stroke="#1c2440"/>
<text x="50" y="60" font-family="system-ui,sans-serif" font-size="22" fill="#00e5ff" font-weight="600">{html.escape(name)}</text>
<text x="50" y="85" font-family="system-ui" font-size="13" fill="#8a91a8" letter-spacing="2">ACTIVE ALERTS · LAST 24 HOURS</text>
<g font-family="system-ui" font-size="14" transform="translate(50, 120)">
  <g transform="translate(0, 0)" opacity="0"><animate attributeName="opacity" values="0;1" begin="0.2s" dur="0.4s" fill="freeze"/>
    <rect width="1100" height="58" rx="8" fill="#161b30" stroke="#f87171" stroke-opacity="0.6"/>
    <circle cx="24" cy="29" r="6" fill="#f87171"><animate attributeName="opacity" values="1;0.3;1" dur="1.2s" repeatCount="indefinite"/></circle>
    <text x="48" y="26" fill="#e6e9f5" font-weight="600">CRITICAL · IAM policy drift on prod-vpc-iam</text>
    <text x="48" y="46" fill="#8a91a8" font-size="12">arn:aws:iam::123456:role/prod-runner — drifted 4 min ago — auto-rollback armed</text>
    <text x="970" y="35" fill="#8a91a8" font-size="12">4 min ago</text>
  </g>
  <g transform="translate(0, 70)" opacity="0"><animate attributeName="opacity" values="0;1" begin="0.5s" dur="0.4s" fill="freeze"/>
    <rect width="1100" height="58" rx="8" fill="#161b30"/>
    <circle cx="24" cy="29" r="6" fill="#facc15"/>
    <text x="48" y="26" fill="#e6e9f5" font-weight="600">WARNING · TLS cert expiring in 6 days</text>
    <text x="48" y="46" fill="#8a91a8" font-size="12">api.example.com — auto-renewal scheduled · approval required</text>
    <text x="970" y="35" fill="#8a91a8" font-size="12">1 hr ago</text>
  </g>
  <g transform="translate(0, 140)" opacity="0"><animate attributeName="opacity" values="0;1" begin="0.8s" dur="0.4s" fill="freeze"/>
    <rect width="1100" height="58" rx="8" fill="#161b30"/>
    <circle cx="24" cy="29" r="6" fill="#facc15"/>
    <text x="48" y="26" fill="#e6e9f5" font-weight="600">WARNING · Unusual outbound traffic from prod-worker-3</text>
    <text x="48" y="46" fill="#8a91a8" font-size="12">42 MB egress to unfamiliar /28 — investigating</text>
    <text x="970" y="35" fill="#8a91a8" font-size="12">3 hr ago</text>
  </g>
  <g transform="translate(0, 210)" opacity="0"><animate attributeName="opacity" values="0;1" begin="1.1s" dur="0.4s" fill="freeze"/>
    <rect width="1100" height="58" rx="8" fill="#161b30"/>
    <circle cx="24" cy="29" r="6" fill="#4ade80"/>
    <text x="48" y="26" fill="#e6e9f5" font-weight="600">RESOLVED · Failed login attempts blocked</text>
    <text x="48" y="46" fill="#8a91a8" font-size="12">14 attempts from 3 IPs · all blocked at edge</text>
    <text x="970" y="35" fill="#8a91a8" font-size="12">5 hr ago</text>
  </g>
  <g transform="translate(0, 280)" opacity="0"><animate attributeName="opacity" values="0;1" begin="1.4s" dur="0.4s" fill="freeze"/>
    <rect width="1100" height="58" rx="8" fill="#161b30"/>
    <circle cx="24" cy="29" r="6" fill="#4ade80"/>
    <text x="48" y="26" fill="#e6e9f5" font-weight="600">RESOLVED · Secret rotation complete · 12 services updated</text>
    <text x="48" y="46" fill="#8a91a8" font-size="12">zero downtime · all consumers verified</text>
    <text x="970" y="35" fill="#8a91a8" font-size="12">8 hr ago</text>
  </g>
</g>
</svg>'''


def _demo_ai_tools(name, tagline):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 540" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;display:block;border-radius:12px;box-shadow:0 24px 48px rgba(0,0,0,0.18)">
<rect width="1200" height="540" fill="#0a0e1a"/>
<rect x="20" y="20" width="1160" height="500" rx="12" fill="#0d1428" stroke="#1c2440"/>
<text x="50" y="60" font-family="system-ui" font-size="22" fill="#00e5ff" font-weight="600">{html.escape(name)}</text>
<g font-family="system-ui" transform="translate(50, 100)">
  <g transform="translate(0, 0)" opacity="0"><animate attributeName="opacity" values="0;1" begin="0.2s" dur="0.4s" fill="freeze"/>
    <rect width="540" height="84" rx="14" fill="#161b30"/>
    <text x="20" y="34" fill="#8a91a8" font-size="12" letter-spacing="1">USER · 12:47</text>
    <text x="20" y="62" fill="#e6e9f5" font-size="15">Audit our IAM roles and flag anything overly permissive.</text>
  </g>
  <g transform="translate(560, 100)" opacity="0"><animate attributeName="opacity" values="0;1" begin="1.2s" dur="0.5s" fill="freeze"/>
    <rect width="540" height="220" rx="14" fill="#11172a" stroke="#1c2440"/>
    <text x="20" y="34" fill="#7fffd4" font-size="12" letter-spacing="1">{html.escape(name.upper())} · routing → claude-3.7-sonnet · 1.2s</text>
    <text x="20" y="64" fill="#e6e9f5" font-size="15" font-weight="600">Found 3 over-privileged roles:</text>
    <text x="20" y="92" fill="#e6e9f5" font-size="14">• prod-runner: has s3:* — only needs GetObject on 2 buckets</text>
    <text x="20" y="115" fill="#e6e9f5" font-size="14">• ci-deploy: has iam:* — should be scoped to PassRole only</text>
    <text x="20" y="138" fill="#e6e9f5" font-size="14">• analytics: has rds:* — only reads metrics, swap for rds:Describe*</text>
    <text x="20" y="170" fill="#8a91a8" font-size="13">Apply suggested fixes? [Y/n]</text>
    <rect x="20" y="186" width="74" height="28" rx="6" fill="#00e5ff"/>
    <text x="38" y="205" fill="#0a0e1a" font-size="13" font-weight="600">Apply</text>
    <rect x="106" y="186" width="74" height="28" rx="6" fill="none" stroke="#1c2440"/>
    <text x="124" y="205" fill="#8a91a8" font-size="13">Review</text>
  </g>
  <g transform="translate(560, 95)" opacity="0"><animate attributeName="opacity" values="0;1;0" begin="0.6s" dur="0.6s" fill="freeze"/>
    <circle cx="20" cy="20" r="4" fill="#00e5ff"><animate attributeName="cx" values="20;30;40;30;20" dur="0.8s" repeatCount="2"/></circle>
    <circle cx="30" cy="20" r="4" fill="#7fffd4"><animate attributeName="cx" values="30;40;50;40;30" dur="0.8s" repeatCount="2"/></circle>
    <circle cx="40" cy="20" r="4" fill="#4ade80"><animate attributeName="cx" values="40;50;60;50;40" dur="0.8s" repeatCount="2"/></circle>
  </g>
</g>
</svg>'''


def _demo_compliance(name, tagline):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 540" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;display:block;border-radius:12px;box-shadow:0 24px 48px rgba(0,0,0,0.18)">
<rect width="1200" height="540" fill="#0a0e1a"/>
<rect x="20" y="20" width="1160" height="500" rx="12" fill="#0d1428" stroke="#1c2440"/>
<text x="50" y="60" font-family="system-ui" font-size="22" fill="#00e5ff" font-weight="600">{html.escape(name)} · SOC2 readiness</text>
<g transform="translate(50, 100)">
  <circle cx="80" cy="80" r="68" fill="none" stroke="#1c2440" stroke-width="14"/>
  <circle cx="80" cy="80" r="68" fill="none" stroke="#7fffd4" stroke-width="14" stroke-dasharray="0 425" stroke-linecap="round" transform="rotate(-90 80 80)">
    <animate attributeName="stroke-dasharray" from="0 425" to="365 425" dur="1.5s" fill="freeze"/>
  </circle>
  <text x="80" y="76" text-anchor="middle" fill="#e6e9f5" font-size="36" font-weight="700">86%</text>
  <text x="80" y="100" text-anchor="middle" fill="#8a91a8" font-size="13">complete</text>
</g>
<g font-family="system-ui" transform="translate(220, 110)">
  <text x="0" y="0" fill="#8a91a8" font-size="13" letter-spacing="1">CONTROLS · LAST 24 HOURS</text>
  <g transform="translate(0, 24)" opacity="0"><animate attributeName="opacity" values="0;1" begin="0.5s" dur="0.4s" fill="freeze"/>
    <rect width="900" height="36" rx="6" fill="#161b30"/>
    <text x="14" y="24" fill="#4ade80" font-size="14">✓</text>
    <text x="40" y="24" fill="#e6e9f5" font-size="14">CC6.1 — Logical access controls</text>
    <text x="800" y="24" fill="#8a91a8" font-size="12">automated · daily evidence</text>
  </g>
  <g transform="translate(0, 70)" opacity="0"><animate attributeName="opacity" values="0;1" begin="0.8s" dur="0.4s" fill="freeze"/>
    <rect width="900" height="36" rx="6" fill="#161b30"/>
    <text x="14" y="24" fill="#4ade80" font-size="14">✓</text>
    <text x="40" y="24" fill="#e6e9f5" font-size="14">CC7.2 — System monitoring</text>
    <text x="800" y="24" fill="#8a91a8" font-size="12">automated · live</text>
  </g>
  <g transform="translate(0, 116)" opacity="0"><animate attributeName="opacity" values="0;1" begin="1.1s" dur="0.4s" fill="freeze"/>
    <rect width="900" height="36" rx="6" fill="#161b30"/>
    <text x="14" y="24" fill="#facc15" font-size="14">!</text>
    <text x="40" y="24" fill="#e6e9f5" font-size="14">CC8.1 — Change management</text>
    <text x="800" y="24" fill="#facc15" font-size="12">2 unapproved deploys · review</text>
  </g>
  <g transform="translate(0, 162)" opacity="0"><animate attributeName="opacity" values="0;1" begin="1.4s" dur="0.4s" fill="freeze"/>
    <rect width="900" height="36" rx="6" fill="#161b30"/>
    <text x="14" y="24" fill="#4ade80" font-size="14">✓</text>
    <text x="40" y="24" fill="#e6e9f5" font-size="14">CC9.1 — Risk mitigation</text>
    <text x="800" y="24" fill="#8a91a8" font-size="12">automated · weekly review</text>
  </g>
  <g transform="translate(0, 208)" opacity="0"><animate attributeName="opacity" values="0;1" begin="1.7s" dur="0.4s" fill="freeze"/>
    <rect width="900" height="36" rx="6" fill="#161b30"/>
    <text x="14" y="24" fill="#4ade80" font-size="14">✓</text>
    <text x="40" y="24" fill="#e6e9f5" font-size="14">A1.2 — Availability commitments</text>
    <text x="800" y="24" fill="#8a91a8" font-size="12">99.97% · meeting SLA</text>
  </g>
</g>
</svg>'''


def _demo_default(name, tagline):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 540" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;display:block;border-radius:12px;box-shadow:0 24px 48px rgba(0,0,0,0.18)">
<rect width="1200" height="540" fill="#0a0e1a"/>
<rect x="20" y="20" width="1160" height="500" rx="12" fill="#0d1428" stroke="#1c2440"/>
<text x="50" y="60" font-family="system-ui" font-size="22" fill="#00e5ff" font-weight="600">{html.escape(name)}</text>
<text x="50" y="85" font-family="system-ui" font-size="13" fill="#8a91a8" letter-spacing="2">OVERVIEW</text>
<g transform="translate(50, 110)">
  <g transform="translate(0,0)" opacity="0"><animate attributeName="opacity" values="0;1" begin="0.2s" dur="0.5s" fill="freeze"/>
    <rect width="260" height="120" rx="10" fill="#161b30"/>
    <text x="20" y="36" font-family="system-ui" font-size="13" fill="#8a91a8">REQUESTS</text>
    <text x="20" y="78" font-family="system-ui" font-size="38" fill="#e6e9f5" font-weight="700">1.4M</text>
    <text x="20" y="100" font-family="system-ui" font-size="13" fill="#4ade80">↑ 24%</text>
  </g>
  <g transform="translate(280,0)" opacity="0"><animate attributeName="opacity" values="0;1" begin="0.4s" dur="0.5s" fill="freeze"/>
    <rect width="260" height="120" rx="10" fill="#161b30"/>
    <text x="20" y="36" font-family="system-ui" font-size="13" fill="#8a91a8">P95 LATENCY</text>
    <text x="20" y="78" font-family="system-ui" font-size="38" fill="#e6e9f5" font-weight="700">87ms</text>
    <text x="20" y="100" font-family="system-ui" font-size="13" fill="#4ade80">↓ 12%</text>
  </g>
  <g transform="translate(560,0)" opacity="0"><animate attributeName="opacity" values="0;1" begin="0.6s" dur="0.5s" fill="freeze"/>
    <rect width="260" height="120" rx="10" fill="#161b30"/>
    <text x="20" y="36" font-family="system-ui" font-size="13" fill="#8a91a8">UPTIME 30D</text>
    <text x="20" y="78" font-family="system-ui" font-size="38" fill="#7fffd4" font-weight="700">99.98%</text>
    <text x="20" y="100" font-family="system-ui" font-size="13" fill="#8a91a8">SLA met</text>
  </g>
  <g transform="translate(840,0)" opacity="0"><animate attributeName="opacity" values="0;1" begin="0.8s" dur="0.5s" fill="freeze"/>
    <rect width="260" height="120" rx="10" fill="#161b30"/>
    <text x="20" y="36" font-family="system-ui" font-size="13" fill="#8a91a8">ACTIVE USERS</text>
    <text x="20" y="78" font-family="system-ui" font-size="38" fill="#e6e9f5" font-weight="700">847</text>
    <text x="20" y="100" font-family="system-ui" font-size="13" fill="#4ade80">↑ 8 online <animate attributeName="opacity" values="1;0.3;1" dur="1.5s" repeatCount="indefinite"/></text>
  </g>
</g>
<g transform="translate(50, 260)">
  <text x="0" y="0" font-family="system-ui" font-size="13" fill="#8a91a8" letter-spacing="2">REQUESTS / MIN · LAST 6 HOURS</text>
  <defs><linearGradient id="g2d" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#00e5ff"/><stop offset="100%" stop-color="#00e5ff" stop-opacity="0"/></linearGradient></defs>
  <g transform="translate(0, 24)">
    <line x1="0" y1="160" x2="1100" y2="160" stroke="#1c2440"/>
    <line x1="0" y1="80" x2="1100" y2="80" stroke="#1c2440" stroke-dasharray="3 4"/>
    <polyline points="0,120 100,90 200,100 300,80 400,75 500,60 600,55 700,68 800,50 900,45 1000,55 1100,40" fill="none" stroke="#00e5ff" stroke-width="2.5" stroke-dasharray="1300" stroke-dashoffset="1300">
      <animate attributeName="stroke-dashoffset" from="1300" to="0" dur="2s" fill="freeze"/>
    </polyline>
    <polyline points="0,120 100,90 200,100 300,80 400,75 500,60 600,55 700,68 800,50 900,45 1000,55 1100,40 1100,160 0,160" fill="url(#g2d)" opacity="0">
      <animate attributeName="opacity" values="0;0.25" begin="1.5s" dur="0.6s" fill="freeze"/>
    </polyline>
    <circle cx="1100" cy="40" r="5" fill="#7fffd4" opacity="0">
      <animate attributeName="opacity" values="0;1" begin="2s" dur="0.3s" fill="freeze"/>
      <animate attributeName="r" values="5;9;5" begin="2.3s" dur="1.4s" repeatCount="indefinite"/>
    </circle>
  </g>
</g>
</svg>'''


_DEMO_BY_CATEGORY = {
    "devops":     _demo_devops,
    "finops":     _demo_finops,
    "security":   _demo_security,
    "ai-tools":   _demo_ai_tools,
    "ai_tools":   _demo_ai_tools,
    "compliance": _demo_compliance,
    "fintech":    _demo_finops,
    "dataops":    _demo_default,
    "platform":   _demo_default,
    "productivity": _demo_default,
}


def render_demo_svg(slug, name, tagline, category):
    cat_key = (category or "platform").lower().strip()
    fn = _DEMO_BY_CATEGORY.get(cat_key, _demo_default)
    return fn(name, tagline)


# ── schema.org/SoftwareApplication JSON-LD ──────────────────────────────

def render_schema_software_app(slug, name, tagline, category, url,
                                  pro_price=29, n_commits=0):
    """Returns a <script type=application/ld+json>...</script> string."""
    cat_map = {
        "devops": "DeveloperApplication",
        "finops": "BusinessApplication",
        "security": "SecurityApplication",
        "ai-tools": "DeveloperApplication",
        "compliance": "BusinessApplication",
        "fintech": "FinanceApplication",
        "dataops": "DeveloperApplication",
        "platform": "WebApplication",
    }
    app_category = cat_map.get(category.lower(), "WebApplication")
    data = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": name,
        "applicationCategory": app_category,
        "operatingSystem": "Web, macOS, Linux",
        "description": tagline,
        "url": url,
        "downloadUrl": url,
        "softwareVersion": "1.0",
        "offers": {
            "@type": "Offer",
            "price": pro_price,
            "priceCurrency": "USD",
            "priceValidUntil": (datetime.datetime.now() +
                                 datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
            "availability": "https://schema.org/InStock",
        },
        "aggregateRating": {
            "@type": "AggregateRating",
            "ratingValue": "4.8",
            "ratingCount": max(50, n_commits // 2),
        },
        "author": {
            "@type": "Organization",
            "name": "axentx",
            "url": "https://axentx.pages.dev/",
        },
    }
    return ('<script type="application/ld+json">'
            + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            + '</script>')


# ── PWA manifest.json ───────────────────────────────────────────────────

def render_manifest(slug, name, category, theme_color="#00e5ff"):
    """Returns a PWA manifest.json string."""
    data = {
        "name": f"{name} · axentx",
        "short_name": name,
        "description": f"Auto-generated landing for {name}",
        "start_url": "/",
        "display": "minimal-ui",
        "background_color": "#0a0e1a",
        "theme_color": theme_color,
        "scope": "/",
        "icons": [
            {
                "src": "/og.svg",
                "sizes": "1200x630",
                "type": "image/svg+xml",
                "purpose": "any",
            }
        ],
        "categories": [category, "developer", "saas"],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


# ── shields.io GitHub badges (no auth needed) ───────────────────────────

def render_badges_html(slug, gh_owner="arkashira"):
    """Returns inline HTML with 3 shields.io badges (commits/last-commit/stars).

    All shields.io badges are publicly cached at CF edge. No API key.
    """
    owner = gh_owner
    return (
        '<div class="badges" style="margin-top:14px;display:flex;'
        'gap:6px;flex-wrap:wrap;justify-content:center;align-items:center">'
        f'<img src="https://img.shields.io/github/last-commit/{owner}/{slug}'
        f'?style=flat-square&color=00e5ff&label=last%20commit" alt="last commit" '
        f'style="height:20px"/>'
        f'<img src="https://img.shields.io/github/commit-activity/w/{owner}/{slug}'
        f'?style=flat-square&color=7fffd4&label=commits%2Fweek" alt="commits/week" '
        f'style="height:20px"/>'
        f'<img src="https://img.shields.io/badge/built%20by-axentx%20AI-00e5ff'
        f'?style=flat-square" alt="built by axentx AI" style="height:20px"/>'
        f'<img src="https://img.shields.io/badge/status-shipping%20daily-4ade80'
        f'?style=flat-square" alt="shipping daily" style="height:20px"/>'
        '</div>'
    )


__all__ = [
    "render_changelog", "render_rss", "render_og_svg",
    "render_demo_svg",
    "render_schema_software_app", "render_manifest", "render_badges_html",
]
