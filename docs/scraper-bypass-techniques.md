# Scraper bypass techniques — survey + what we use

> "หา proxy agent หรือ solution bypass การพิสูจตัวตน ... รวมไปถึงหาจาก
>  github แบบ deep deep" — ฟิวส์, 2026-05-02

This is a working survey of techniques to scrape behind anti-bot walls
on a free-tier shared-NAT IP (where every datacenter UA gets 403 on
sight). Each row marks **adopted / planned / rejected** with rationale.

| Technique | Cost | Effort | Reliability | Status | Notes |
|---|---|---|---|---|---|
| **OAuth official API** | free if quota | low | very high | ✅ adopted | Reddit OAuth (script-app, 600 req/10min). GitHub Issues (60 req/h authenticated). Stack Exchange (300 req/day no auth). |
| **Browser-style UA** | free | trivial | medium | ✅ adopted | Hermes UA = Chrome 120 desktop. Bypasses naive UA-block on most CDNs. Fails against CF managed challenge / Akamai. |
| **Honor Retry-After + global backoff** | free | low | high | ✅ adopted | hf-dataset-discoverer + watchdog — tracks `_HF_BACKOFF_UNTIL` + per-repo cooldown. Stops 272k 429/7d storms. |
| **Per-source rotation** | free | low | high | ✅ adopted | research-daemon cursor advances across 31 sources. One source blocked ≠ pipeline dead. |
| **Public RSS feeds** | free | trivial | medium | ✅ adopted | Indie Hackers, ProductHunt — slower update than API but uncensored. |
| **Lobsters / dev.to / HN APIs** | free | low | very high | ✅ adopted | First-party JSON APIs, no auth, no rate-limit-of-note. |
| **archive.org Wayback** | free | medium | medium | 🔜 planned | Snapshot lookup `https://web.archive.org/web/2*/<url>` — useful for pages now blocked or deleted. |
| **archive.today** | free | medium | medium | 🔜 planned | Same idea, different snapshot pool. Often has the content archive.org doesn't. |
| **Pushshift-alternatives for Reddit** | free | medium | low (defunct) | ❌ rejected | Original Pushshift killed June 2023; the alternatives (arctic_shift, zwiep.com) are unstable + lag months. OAuth is better. |
| **Scrape via CF Worker (egress IP rotation)** | free | medium | high | 🔜 planned | Worker `fetch()` from CF's egress IPs ≠ GCP NAT — different rate-limit pool. Built one tier of fan-out for `/probe?url=<>` route to be implemented. |
| **Crawl4AI** | free, OSS | medium | high | 🔜 planned | Python framework w/ stealth Playwright. Heavy (Chromium binary), but bypasses CF managed challenge + JS-walled SPAs. Use sparingly for high-value targets. |
| **Playwright-stealth (raw)** | free | high | high | ❌ rejected | Bare Playwright + stealth plugins works but heavy + brittle. Crawl4AI wraps it cleanly — go through that. |
| **undetected-chromedriver** | free | medium | medium | ❌ rejected | Selenium-based, lags real Chrome, gets fingerprinted. Crawl4AI is the modern replacement. |
| **Rotating residential proxies** | $5-50/mo | low | very high | ❌ rejected (cost) | Bright Data / Oxylabs / Smartproxy. Solves everything but breaks the "free tier only" constraint. Reconsider once revenue. |
| **Tor exit nodes** | free | low | low | ❌ rejected | Most CDNs hard-block Tor exits. Plus latency makes pipeline crawl. |
| **Cloudflare Browser Rendering** | free 1k/day | medium | high | 🔜 planned | New CF service, evaluating. Same egress as Workers, plus headless Chrome. Could replace Crawl4AI for low-volume high-value scrapes. |
| **Multi-VM IP rotation** | free | low | medium | 🔜 in progress | OCI A1 Singapore + GCP us-central1 + (if needed) Render Frankfurt = 3 distinct datacenter IPs. Site that 429s GCP often serves OCI fine. axentx-research-daemon@1..3 already supports per-worker source rotation; once OCI joins, run @1@2 on GCP and @3 on OCI. |
| **Local instance / mobile hotspot** | free | very high | very high | ❌ not viable | Manual operator effort; not a daemon-driven path. Kept here for the record. |
| **Site-specific: 'old.<site>.com'** | free | trivial | medium | ✅ adopted | old.reddit.com bypasses some anti-scrape that hits www. Keep as fallback in fetch_reddit. |

## Prioritized roadmap (next 3 milestones)

### Milestone 1 — wider-net adoption (immediate)
- [x] Reddit OAuth in research-daemon
- [x] StackExchange API
- [x] GitHub Issues search API
- [x] ProductHunt RSS
- [x] Browser UA + Retry-After globally
- [ ] **archive.org / archive.today snapshot fallback** for any URL that
      404/410s — research-daemon falls through to snapshot before giving up.

### Milestone 2 — egress IP diversity (this week)
- [ ] OCI A1 (or E2.1.Micro fallback) provisioned — gives us a 2nd
      datacenter IP. axentx-research-daemon@3 will run there → for sites
      that block GCP IP, OCI worker has fresh quota.
- [ ] CF Worker `/probe?url=<>` — fan-out from CF edge for low-volume
      high-value URLs (e.g. paywalled academic citations during pain
      validation).

### Milestone 3 — heavy artillery (when needed)
- [ ] Crawl4AI integration: a `axentx-crawl4ai-daemon` invoked ON DEMAND
      by research-daemon ONLY for URLs flagged as high-value AND
      blocked by lighter techniques. Keeps Chromium overhead off the
      hot path.
- [ ] CF Browser Rendering: drop-in replacement for Crawl4AI if it
      proves cheaper for our access pattern.

## What we DON'T do (and why)

- Build our own anti-bot bypass (header/JS fingerprint masking, TLS
  ja3 randomization). Time-sink, brittle, gets defeated within weeks
  every time the target updates. Use OAuth+APIs first; only fall back
  to a maintained framework (Crawl4AI) for the long tail.
- Pay for residential proxies. Breaks the free-tier constraint that
  defines this project's economics. Revisit when ARR > 0.
- Manual scraping by operator. Not a daemon-driven path. If a site is
  worth scraping we either find an API or accept the gap.

---

Updated 2026-05-02 — running list, edited as we add or retire techniques.
