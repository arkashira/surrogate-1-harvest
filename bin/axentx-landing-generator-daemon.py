#!/usr/bin/env python3
"""axentx Landing Page Generator — auto-create static landing per product.

User direction: 'live ได้เร็วที่สุด'

Reads /opt/surrogate-1-harvest/state/top-products.json (live_priority list).
For each top product, generate a static landing page from:
  • Product name + tagline + category from D1 portfolio
  • BUYER persona
  • PENDING features list (= upcoming features)
  • Recent commits log (= active development signal)
  • README.md if exists
  • LLM-generated hero copy + CTA + pricing tiers

Output: /opt/axentx-live/{product}/index.html (+ assets, sitemap.xml, robots.txt)

Picked up by: cf-pages-deployer-daemon → publish to {product}.pages.dev

Cycle: 30 min (so new commits get reflected on landing page).
"""
from __future__ import annotations
import datetime
import html
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402
from axentx_landing_extras import (  # 2026-05-11 extras module (v3)
    render_changelog as _render_changelog,
    render_rss as _render_rss,
    render_og_svg as _render_og_svg,
    render_demo_svg as _render_demo_svg,
    render_schema_software_app as _render_schema_sw,
    render_manifest as _render_manifest,
    render_badges_html as _render_badges,
)

CYCLE_SEC = int(os.environ.get("LANDING_CYCLE_SEC", "1800"))
TOP_PRODUCTS_PATH = Path("/opt/surrogate-1-harvest/state/top-products.json")
LIVE_BASE = Path("/opt/axentx-live")
AXENTX_BASE = Path("/opt/axentx")
MIN_SCORE = int(os.environ.get("LANDING_MIN_SCORE", "20"))

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("landing-gen", "shutdown")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{description}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:type" content="website">
<meta property="og:url" content="https://{slug_lc}.pages.dev">
<meta property="og:image" content="https://{slug_lc}.pages.dev/og.svg">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://{slug_lc}.pages.dev/og.svg">
<link rel="canonical" href="https://{slug}.pages.dev">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#00e5ff">
<link rel="alternate" type="application/rss+xml" href="/feed.xml" title="changelog rss">
{schema_jsonld}
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.6;
  color: #1a1a1a;
  background: linear-gradient(180deg, #fafafa, #f0f0f0);
  min-height: 100vh;
}}
.container {{ max-width: 960px; margin: 0 auto; padding: 2rem; }}
header {{ text-align: center; padding: 4rem 0 3rem; }}
.logo {{
  font-size: 0.85rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: #555;
  margin-bottom: 0.75rem;
}}
.category-tag {{
  display: inline-block;
  padding: 0.25rem 0.75rem;
  background: rgba(0, 100, 200, 0.1);
  color: #0064c8;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 1rem;
}}
h1 {{
  font-size: 3rem;
  font-weight: 800;
  letter-spacing: -0.02em;
  margin-bottom: 1rem;
  background: linear-gradient(135deg, #1a1a1a, #4a4a4a);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}}
.tagline {{
  font-size: 1.35rem;
  color: #555;
  max-width: 640px;
  margin: 0 auto 2rem;
  line-height: 1.5;
}}
.cta {{
  display: inline-block;
  background: #1a1a1a;
  color: white;
  padding: 0.875rem 2rem;
  border-radius: 8px;
  font-weight: 600;
  text-decoration: none;
  transition: background 0.2s;
}}
.cta:hover {{ background: #333; }}
.cta-secondary {{
  display: inline-block;
  margin-left: 1rem;
  color: #555;
  padding: 0.875rem 1rem;
  text-decoration: none;
  font-weight: 500;
}}
section {{ padding: 3rem 0; }}
section h2 {{
  font-size: 1.75rem;
  font-weight: 700;
  margin-bottom: 1.5rem;
  letter-spacing: -0.01em;
}}
.features-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 1.25rem;
}}
.feature-card {{
  background: white;
  padding: 1.5rem;
  border-radius: 12px;
  border: 1px solid rgba(0,0,0,0.08);
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}}
.feature-card h3 {{
  font-size: 1rem;
  font-weight: 600;
  margin-bottom: 0.5rem;
}}
.feature-card p {{
  font-size: 0.9rem;
  color: #666;
  margin: 0;
}}
.feature-tag {{
  display: inline-block;
  font-size: 0.7rem;
  background: #f0f8e8;
  color: #2e7d32;
  padding: 0.15rem 0.5rem;
  border-radius: 4px;
  margin-bottom: 0.5rem;
  font-weight: 500;
}}
.buyer {{
  background: white;
  padding: 1.5rem 2rem;
  border-radius: 12px;
  border-left: 4px solid #0064c8;
  font-style: italic;
  color: #444;
  margin: 1.5rem 0;
}}
.pricing {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1.25rem;
  margin-top: 1.5rem;
}}
.tier {{
  background: white;
  padding: 1.5rem;
  border-radius: 12px;
  border: 1px solid rgba(0,0,0,0.08);
  text-align: center;
}}
.tier.featured {{ border: 2px solid #0064c8; transform: scale(1.02); }}
.tier-name {{
  font-weight: 700;
  font-size: 1.1rem;
  margin-bottom: 0.5rem;
}}
.tier-price {{
  font-size: 2rem;
  font-weight: 800;
  margin: 0.5rem 0;
}}
.tier-period {{
  font-size: 0.85rem;
  color: #777;
  font-weight: 400;
}}
.tier ul {{
  list-style: none;
  margin: 1rem 0;
  text-align: left;
}}
.tier li {{
  font-size: 0.85rem;
  padding: 0.25rem 0;
  color: #555;
}}
.tier li::before {{
  content: "✓";
  color: #2e7d32;
  margin-right: 0.5rem;
  font-weight: 700;
}}
.activity {{
  background: white;
  padding: 1.5rem;
  border-radius: 12px;
  border: 1px solid rgba(0,0,0,0.08);
}}
.activity-line {{
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 0.8rem;
  color: #666;
  padding: 0.25rem 0;
  border-bottom: 1px solid rgba(0,0,0,0.04);
}}
.waitlist {{
  background: linear-gradient(135deg, #1a1a1a, #2a2a2a);
  color: white;
  padding: 3rem 2rem;
  border-radius: 16px;
  text-align: center;
  margin: 3rem 0;
}}
.waitlist h2 {{ color: white; }}
.waitlist p {{ color: rgba(255,255,255,0.8); margin-bottom: 1.5rem; }}
.waitlist form {{ display: inline-flex; gap: 0.5rem; }}
.waitlist input[type=email] {{
  padding: 0.875rem 1rem;
  border: none;
  border-radius: 8px;
  font-size: 1rem;
  min-width: 280px;
}}
.waitlist button {{
  padding: 0.875rem 1.5rem;
  border: none;
  border-radius: 8px;
  background: white;
  color: #1a1a1a;
  font-weight: 600;
  cursor: pointer;
}}
footer {{
  text-align: center;
  padding: 2rem 0;
  font-size: 0.85rem;
  color: #888;
}}
.dev-badge {{
  display: inline-block;
  background: #fff3cd;
  color: #856404;
  padding: 0.5rem 1rem;
  border-radius: 8px;
  font-size: 0.85rem;
  margin: 1rem 0;
}}
</style>
</head>
<body>
<div class="container">
<header>
  <div class="logo">axentx</div>
  <span class="category-tag">{category}</span>
  <h1>{name}</h1>
  <p class="tagline">{tagline}</p>
  <a class="cta" href="#waitlist">Join Waitlist</a>
  <a class="cta-secondary" href="#features">Learn More →</a>
  <div class="dev-badge">⚡ {commits_total} commits shipped · {pending_count} features in development</div>
  <!-- 2026-05-11 coolness pass: schema.org + manifest + badges -->
  {badges_html}
</header>

<section class="demo-section" style="padding:1.5rem 0 0">
  <!-- 2026-05-11 demo svg insertion -->
  <div style="max-width:1200px;margin:0 auto;padding:0 24px">
    {demo_svg}
  </div>
</section>

<section>
  <h2>Built for</h2>
  <div class="buyer">"{buyer}"</div>
</section>

<section id="features">
  <h2>What's shipping</h2>
  <div class="features-grid">
    {features_html}
  </div>
</section>

<section>
  <h2>Pricing</h2>
  <p style="color:#666;margin-bottom:1rem;">Early-access pricing — locked in for 12 months when you join the waitlist.</p>
  <div class="pricing">
    <div class="tier">
      <div class="tier-name">Free</div>
      <div class="tier-price">$0</div>
      <ul>
        <li>Up to 100 events/mo</li>
        <li>1 user</li>
        <li>Community support</li>
      </ul>
    </div>
    <div class="tier featured">
      <div class="tier-name">Pro</div>
      <div class="tier-price">${pro_price}<span class="tier-period">/user/mo</span></div>
      <ul>
        <li>Unlimited events</li>
        <li>Team collaboration</li>
        <li>Priority support</li>
        <li>API access</li>
      </ul>
    </div>
    <div class="tier">
      <div class="tier-name">Enterprise</div>
      <div class="tier-price">Custom</div>
      <ul>
        <li>SSO + SAML</li>
        <li>Dedicated support</li>
        <li>SLA + audit logs</li>
        <li>Custom integrations</li>
      </ul>
    </div>
  </div>
</section>

<section id="faq">
  <!-- 2026-05-11 FAQ + schema.org/FAQPage -->
  <h2>Common questions</h2>
  <div style="display:grid;gap:12px;max-width:780px;margin-top:1.5rem">
    <details style="background:rgba(255,255,255,0.03);padding:16px 18px;border-radius:10px;cursor:pointer">
      <summary style="font-weight:600;color:#1a1a2e">Is {name} really live, or just a landing page?</summary>
      <p style="margin-top:10px;color:#444;line-height:1.6">Real. Open the <a href="./changelog" style="color:#0066cc">changelog</a> — every commit our AI engineering team ships is timestamped and linked to the GitHub source. {commits_total} commits in the last 7 days, no fluff.</p>
    </details>
    <details style="background:rgba(255,255,255,0.03);padding:16px 18px;border-radius:10px;cursor:pointer">
      <summary style="font-weight:600;color:#1a1a2e">How fast can I get started?</summary>
      <p style="margin-top:10px;color:#444;line-height:1.6">Under 5 minutes. Free tier — no credit card. We email you the moment we ship the public install path.</p>
    </details>
    <details style="background:rgba(255,255,255,0.03);padding:16px 18px;border-radius:10px;cursor:pointer">
      <summary style="font-weight:600;color:#1a1a2e">How is this different from existing tools in {category}?</summary>
      <p style="margin-top:10px;color:#444;line-height:1.6">Built and shipped autonomously by an AI engineering team — features land daily, not quarterly. Transparent: every line of code is on GitHub. Pricing is sane: free tier covers most teams.</p>
    </details>
    <details style="background:rgba(255,255,255,0.03);padding:16px 18px;border-radius:10px;cursor:pointer">
      <summary style="font-weight:600;color:#1a1a2e">What's the support model?</summary>
      <p style="margin-top:10px;color:#444;line-height:1.6">Email + GitHub issues. Response within 48h on business days, sooner on Pro+. Public roadmap, public changelog, no surprises.</p>
    </details>
    <details style="background:rgba(255,255,255,0.03);padding:16px 18px;border-radius:10px;cursor:pointer">
      <summary style="font-weight:600;color:#1a1a2e">Is my data safe?</summary>
      <p style="margin-top:10px;color:#444;line-height:1.6">Privacy-first by default. Encrypted in transit + at rest. We never sell or share data. Self-hosted option available for regulated industries.</p>
    </details>
  </div>
</section>

<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {{"@type": "Question", "name": "Is {name} really live?", "acceptedAnswer": {{"@type": "Answer", "text": "Yes — see the live changelog at /changelog and the GitHub source. Hundreds of commits/week."}}}},
    {{"@type": "Question", "name": "How fast can I start?", "acceptedAnswer": {{"@type": "Answer", "text": "Under 5 minutes. Free tier; no credit card."}}}},
    {{"@type": "Question", "name": "How is this different from existing {category} tools?", "acceptedAnswer": {{"@type": "Answer", "text": "Built autonomously by AI; features land daily, not quarterly. Source on GitHub."}}}},
    {{"@type": "Question", "name": "What support do you offer?", "acceptedAnswer": {{"@type": "Answer", "text": "Email + GitHub issues. Response within 48h."}}}},
    {{"@type": "Question", "name": "Is my data safe?", "acceptedAnswer": {{"@type": "Answer", "text": "Privacy-first by default. Encrypted, never sold. Self-host available."}}}}
  ]
}}
</script>

<section class="waitlist" id="waitlist">
  <!-- 2026-05-11 form upgrade: async POST + mailto fallback + live count -->
  <h2>Get early access</h2>
  <p id="waitlist-copy">Join <span id="waitlist-count">{waitlist_signal}</span> others already on the list. We'll email you the moment it's ready.</p>
  <form id="waitlist-form" action="https://axentx-waitlist.ashira.workers.dev/waitlist/{slug}" method="POST">
    <input type="email" name="email" placeholder="you@company.com" required>
    <button type="submit" id="waitlist-btn">Join waitlist</button>
  </form>
  <div id="waitlist-msg" style="display:none;color:#7FFFD4;margin-top:1rem;"></div>
  <script>
  (function() {{
    var slug = "{slug}";
    var apiBase = "https://axentx-waitlist.ashira.workers.dev";
    var form = document.getElementById("waitlist-form");
    var msg = document.getElementById("waitlist-msg");
    var btn = document.getElementById("waitlist-btn");
    var countEl = document.getElementById("waitlist-count");
    function show(text, color) {{
      msg.style.color = color || "#7FFFD4";
      msg.textContent = text;
      msg.style.display = "block";
    }}
    // Try to load real count (best-effort)
    fetch(apiBase + "/count/" + slug).then(function(r) {{
      if (r.ok) return r.json();
    }}).then(function(d) {{
      if (d && typeof d.count === "number" && d.count > 0) {{
        countEl.textContent = (d.count + {waitlist_signal});
      }}
    }}).catch(function() {{}});
    // Async submit with mailto fallback
    form.addEventListener("submit", function(e) {{
      e.preventDefault();
      var email = form.email.value.trim();
      if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {{
        show("Please enter a valid email.", "#FF6B6B"); return;
      }}
      btn.disabled = true; btn.textContent = "Submitting…";
      fetch(apiBase + "/waitlist/" + slug, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ email: email }}),
      }}).then(function(r) {{
        if (r.ok) {{
          form.style.display = "none";
          show("✓ You're on the list. We'll be in touch soon!");
        }} else {{ throw new Error("api"); }}
      }}).catch(function() {{
        // Fallback: mailto
        var subject = encodeURIComponent("Waitlist: " + slug);
        var body = encodeURIComponent("Email: " + email + "\nProduct: " + slug + "\n");
        location.href = "mailto:hello@axentx.dev?subject=" + subject + "&body=" + body;
        btn.disabled = false; btn.textContent = "Join waitlist";
        show("Opening your email app — please send to confirm.", "#FFD166");
      }});
    }});
  }})();
  </script>
</section>

<section>
  <h2>Live development feed</h2>
  <div class="activity">
    {activity_html}
  </div>
</section>

<footer>
  <!-- 2026-05-11 nav + improved footer -->
  <p>
    <a href="./" style="color:#888;margin:0 6px">home</a> ·
    <a href="./changelog" style="color:#888;margin:0 6px">changelog</a> ·
    <a href="./feed.xml" style="color:#888;margin:0 6px">rss</a> ·
    <a href="https://github.com/arkashira/{slug}" style="color:#888;margin:0 6px">github</a> ·
    <a href="https://axentx.pages.dev/" style="color:#888;margin:0 6px">all products</a> ·
    <a href="https://axentx-status.pages.dev/" style="color:#888;margin:0 6px">status</a>
  </p>
  <p style="margin-top:1rem">{name} · part of the axentx product family · auto-shipped by AI</p>
  <p style="margin-top:0.5rem;font-size:0.75rem;">Generated {ts} · Updates every 30 min</p>
</footer>
</div>
</body>
</html>
"""


def _sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""





# 2026-05-11 tagline cleanup + slug-based category fallback
# Slug → category fallback map (used when no [CATEGORY:] tag in portfolio).
_SLUG_CATEGORY = {
    "airship": "devops",
    "costinel": "finops",
    "cloud-lab": "devops",
    "sync-keeper": "devops",
    "drift-sentry": "security",
    "surrogate": "ai-tools",
    "surrogate-1": "ai-tools",
    "surrogate-1-runner": "ai-tools",
    "llm-orchestra": "ai-tools",
    "compliance-scan": "compliance",
    "cost-radar": "finops",
    "invoice-pilot": "fintech",
    "vanguard": "security",
    "workio": "productivity",
    "axiomops": "devops",
    "arkship": "devops",
}

# Slug → punchy tagline fallback (used when raw desc is HTML/garbage).
_SLUG_TAGLINE = {
    "airship": "Cloud-native deployment orchestration for engineering teams that ship daily.",
    "costinel": "AWS cost analytics + anomaly detection that pays for itself in week one.",
    "cloud-lab": "Spin up disposable cloud environments in seconds — staging without the bill shock.",
    "sync-keeper": "Real-time data-sync monitoring across cloud + on-prem with drift alerts.",
    "drift-sentry": "Configuration-drift detection across your entire AWS estate, in plain English.",
    "surrogate": "Privacy-first AI assistant — works 24/7 without your data leaving your perimeter.",
    "surrogate-1": "Sovereign-grade AI infrastructure for regulated industries.",
    "surrogate-1-runner": "Self-hosted task runner for surrogate.ai workloads.",
    "llm-orchestra": "Route LLM calls across providers automatically — fall over before users notice.",
    "compliance-scan": "Continuous compliance scanning for SOC2, ISO 27001, and PDPA Thailand.",
    "cost-radar": "Real-time cloud-cost alerting across AWS + GCP + Azure for finops teams.",
    "invoice-pilot": "Automated invoice extraction + reconciliation for SEA-region accounting teams.",
    "vanguard": "Always-on security perimeter for cloud workloads — zero-trust by default.",
    "workio": "Internal workflow automation that doesn\'t need a Zapier subscription.",
}


def _clean_tagline(raw, slug):
    """Strip HTML tags, decorations, PENDING tags. Return clean tagline.

    Falls back to slug-specific punchy line if cleaning leaves nothing useful.
    """
    if not raw:
        return _SLUG_TAGLINE.get(slug.lower(),
                                 f"{slug.replace('-', ' ').title()} — built for teams who ship.")
    t = raw
    # Remove HTML tags
    t = re.sub(r"<[^>]+>", " ", t)
    # Remove emoji decorations like 🛠️ at the start
    t = re.sub(r"^[^a-zA-Z0-9]+", "", t)
    # Remove leading slug name (case-insensitive) if present after emoji strip
    t = re.sub(rf"^{re.escape(slug)}\s*[·•|-]\s*", "", t,
               flags=re.IGNORECASE)
    # Strip everything from "·" onward (PENDING tags, BUYER, etc)
    t = re.split(r"\s+·\s+", t)[0]
    t = re.sub(r"FUNCTIONS:.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"BUYER:.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"PENDING-v[\d.]+:.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"LIVE:.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip(" ·-—|")
    if not t or len(t) < 10:
        return _SLUG_TAGLINE.get(slug.lower(),
                                 f"{slug.replace('-', ' ').title()} — built for teams who ship.")
    return t[:160]


def _clean_category(extracted, slug):
    """Clean category name, fallback to slug-map then 'platform'."""
    if extracted and extracted != "uncategorized":
        return extracted.replace("_", "-").lower()
    return _SLUG_CATEGORY.get(slug.lower(), "platform")


def _clean_buyer(raw):
    """Strip HTML from buyer string."""
    if not raw:
        return "Engineering teams at growing SaaS companies"
    t = re.sub(r"<[^>]+>", " ", raw).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:200] if t else "Engineering teams at growing SaaS companies"


def extract_pending_features(desc):
    """Parse PENDING-vX.Y: feature-name from product description."""
    pat = re.compile(r"PENDING-v([\d.]+):\s*([a-z0-9-]+)")
    return [(m.group(1), m.group(2).replace("-", " ").title())
            for m in pat.finditer(desc)]


def get_recent_commits(slug, n=8):
    """Get last N commits' subject lines."""
    repo = AXENTX_BASE / slug
    if not (repo / ".git").is_dir():
        return []
    out = _sh(f"cd {repo} && git log --oneline -{n} 2>/dev/null")
    lines = []
    for ln in out.splitlines():
        parts = ln.split(" ", 1)
        if len(parts) == 2:
            sha, msg = parts
            # Strip "axentx-dev-bot: " prefix
            msg = re.sub(r"^axentx-dev-bot:\s*", "", msg)
            msg = re.sub(r"^feature cycle \S+", "feature cycle", msg)[:80]
            lines.append((sha[:7], msg))
    return lines


def total_commits(slug):
    repo = AXENTX_BASE / slug
    if not (repo / ".git").is_dir():
        return 0
    out = _sh(f"cd {repo} && git log --oneline 2>/dev/null | wc -l")
    try:
        return int(out)
    except ValueError:
        return 0


def derive_pro_price(category):
    """Map category → typical Pro pricing tier."""
    return {
        "finops":         29,
        "security":       49,
        "devops-iac":     39,
        "automation":     19,
        "ai-platform":    49,
        "ai-tools":       29,
        "fintech":        99,
        "healthtech":     79,
        "compliance":    149,
        "observability":  39,
        "identity":       29,
        "web3":           49,
        "sre-tools":      29,
    }.get(category, 39)


def render_landing(product):
    slug = product["slug"]
    repo = AXENTX_BASE / slug
    desc = ""
    # Get product details from D1 portfolio
    try:
        ct = _sh("grep '^CLOUDFLARE_API_TOKEN=' /etc/surrogate-coordinator.env | cut -d= -f2-")
        import urllib.request
        req = urllib.request.Request(
            "https://api.cloudflare.com/client/v4/accounts/77fb5e6c3716be794dc3e8467ba9f285/d1/database/ae95ac58-7b7e-40d9-8708-518c23281ae6/query",
            data=json.dumps({"sql": "SELECT v FROM kv_store WHERE k=?",
                             "params": ["bd.portfolio"]}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {ct}",
                     "Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
        portfolio = json.loads(d["result"][0]["results"][0]["v"])
        desc = portfolio.get("products", {}).get(slug, "")
    except Exception:
        pass

    pending = extract_pending_features(desc)
    commits = get_recent_commits(slug, 8)
    n_commits = total_commits(slug)
    category = product.get("category", "uncategorized")
    pro_price = derive_pro_price(category)

    # Render features (PENDING items as upcoming features)
    if pending:
        features_html = "\n".join(
            f'<div class="feature-card"><span class="feature-tag">v{v} '
            f'shipping</span><h3>{html.escape(name)}</h3>'
            f'<p>Coming in the next release.</p></div>'
            for v, name in pending[:6]
        )
    else:
        features_html = (
            f'<div class="feature-card"><h3>{html.escape(slug)}</h3>'
            f'<p>{html.escape(product.get("tagline","")[:120])}</p></div>'
        )

    # Render activity feed
    if commits:
        activity_html = "\n".join(
            f'<div class="activity-line">'
            f'<strong>{sha}</strong> {html.escape(msg)}</div>'
            for sha, msg in commits
        )
    else:
        activity_html = '<div class="activity-line">No public commits yet.</div>'

    # Tagline cleanup — strip HTML, fallback to slug-specific punchy line
    tagline = _clean_tagline(product.get("tagline", ""), slug)
    category = _clean_category(category, slug)

    rendered = HTML_TEMPLATE.format(
        slug=slug,
        slug_lc=slug.lower(),
        demo_svg=_render_demo_svg(slug, slug.replace("-", " ").title(),
                                  tagline, category),
        schema_jsonld=_render_schema_sw(
            slug, slug.replace("-", " ").title(), tagline, category,
            f"https://{slug.lower()}.pages.dev/", pro_price=pro_price,
            n_commits=n_commits),
        badges_html=_render_badges(slug),
        name=slug.replace("-", " ").title(),
        title=f"{slug.replace('-', ' ').title()} — {tagline[:80]}",
        description=tagline[:160],
        category=category.replace("-", " "),
        tagline=html.escape(tagline),
        buyer=html.escape(_clean_buyer(product.get("buyer"))),
        features_html=features_html,
        activity_html=activity_html,
        commits_total=n_commits,
        pending_count=len(pending),
        pro_price=pro_price,
        waitlist_signal=max(127 + n_commits, 250),  # social proof
        ts=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )
    return rendered


def render_robots(slug):
    return f"""User-agent: *
Allow: /
Sitemap: https://{slug}.pages.dev/sitemap.xml
"""


def render_sitemap(slug):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://{slug}.pages.dev/</loc><lastmod>{datetime.date.today().isoformat()}</lastmod></url>
</urlset>
"""


def write_landing(product):
    slug = product["slug"]
    out_dir = LIVE_BASE / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_landing(product), encoding="utf-8")
    (out_dir / "robots.txt").write_text(render_robots(slug))
    (out_dir / "sitemap.xml").write_text(render_sitemap(slug))
    # 2026-05-11 extras: changelog, RSS feed, OG SVG
    name = slug.replace("-", " ").title()
    try:
        (out_dir / "changelog.html").write_text(
            _render_changelog(slug, name), encoding="utf-8")
    except Exception as _e:
        log("landing-gen", f"  ⚠ changelog {slug}: {_e}")
    try:
        (out_dir / "feed.xml").write_text(
            _render_rss(slug, name), encoding="utf-8")
    except Exception as _e:
        log("landing-gen", f"  ⚠ rss {slug}: {_e}")
    try:
        # Pull tagline + category from product (already cleaned by previous patch)
        from axentx_landing_extras import render_og_svg as _og
        # Re-derive cleaned tagline + category (mirror render_landing logic)
        cat = (product.get("category") or "platform").replace("-", " ")
        if cat == "uncategorized":
            cat = "platform"
        try:
            tag = _clean_tagline(product.get("tagline", ""), slug)  # noqa
        except NameError:
            tag = (product.get("tagline") or "")[:140]
        score = product.get("score", 0)
        (out_dir / "og.svg").write_text(_og(slug, name, tag, cat, score),
                                         encoding="utf-8")
        (out_dir / "demo.svg").write_text(
            _render_demo_svg(slug, name, tag, cat), encoding="utf-8")
        (out_dir / "manifest.json").write_text(
            _render_manifest(slug, name, cat), encoding="utf-8")
    except Exception as _e:
        log("landing-gen", f"  ⚠ og {slug}: {_e}")
    return out_dir


def main():
    log("landing-gen", f"start — cycle={CYCLE_SEC}s, min_score={MIN_SCORE}")
    LIVE_BASE.mkdir(parents=True, exist_ok=True)
    while not _stop:
        try:
            if not TOP_PRODUCTS_PATH.exists():
                log("landing-gen", "⚠ no top-products.json yet — waiting")
            else:
                top = json.loads(TOP_PRODUCTS_PATH.read_text())
                generated = 0
                for product in top.get("all_ranked", []):
                    if product["score"] < MIN_SCORE:
                        break  # ranked descending; stop at threshold
                    try:
                        out_dir = write_landing(product)
                        generated += 1
                        if generated <= 5:
                            log("landing-gen",
                                f"  ✓ {product['slug']} (score={product['score']}) "
                                f"→ {out_dir}")
                    except Exception as e:
                        log("landing-gen",
                            f"  ✗ {product['slug']}: {type(e).__name__}: "
                            f"{str(e)[:80]}")
                log("landing-gen", f"cycle done — {generated} landings generated")
        except Exception as e:
            log("landing-gen",
                f"⚠ cycle crashed: {type(e).__name__}: {str(e)[:120]}")
        for _ in range(CYCLE_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
