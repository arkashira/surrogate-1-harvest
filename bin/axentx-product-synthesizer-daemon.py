#!/usr/bin/env python3
"""axentx product-synthesizer — proposes NEW product hypotheses
(open-source AND paid) with Thai-vs-global competitor lens.

User feedback 2026-05-04:
  > 'มันต้อง สร้าง ที่ไม่ได้ฝั่ง business ซึ่งก็คือ ที่เป็น opensource ได้
  >  ด้วย แต่งานหลักคือ business ที่ทำเงินได้ มีโอกาส และ ดู competitor
  >  ได้ และ การดู ต้องเทียบในไทย กับ ทั่วโลก เช่น ถ้าในไทยยังโตได้ หรือ
  >  ไม่มีคู่แข่ง ก็น่าสนใจ'

Companion to feature-synthesizer. Where feature-synth EXTENDS existing
products, this daemon proposes brand-new product candidates. They flow
through bd → pitch gate (pre-spawn LLM panel) → only spawn if GO.

Cycle (every 30 min, leader=GCP):
  1. Read recent shared_memory experiences (last 24h) — what kinds of pain
     have agents observed but not yet acted on?
  2. Read shared_kv["bd.portfolio"] — what we already have.
  3. Read shared_kv["llm.providers.health"] — only synth when LLM ≥ 40% up.
  4. Pick 3 candidate domains we DON'T cover (e.g. data observability,
     billing, on-call, FinOps for K8s, niche compliance, ...).
  5. For each: ask LLM to:
     a. Validate market opportunity Thai-side AND global
     b. List top 3 competitors in each market
     c. Identify a wedge (Thai-local-strength OR global-niche)
     d. Output verdict: OPEN-SOURCE / PAID-SAAS / SKIP + rationale
  6. Push survivors as pipeline_items at stage=bd with rich context so
     bd's existing logic + pitch gate can validate them.

Why bd queue (not direct spawn):
  - bd already has overlap-detector (won't duplicate Costinel etc.)
  - pitch gate will kill bad ideas pre-spawn
  - Open-source path allowed (output_mode="open-source" in bd)
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import signal
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_rag_context import attach_rag
from axentx_pipeline import (log, daemon_loop, call_llm,  # noqa: E402
                             get_portfolio, get_portfolio_block)

# Tick interval — fires synth only when bd queue is low. Not a fixed cron.
POLL_SEC = int(os.environ.get("PRODUCT_SYNTH_POLL_SEC", "120"))   # 2-min tick
HOST = socket.gethostname()
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
MAX_NEW_PER_CYCLE = int(os.environ.get("PRODUCT_SYNTH_MAX_PER_CYCLE", "3"))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _llm_health_pct() -> int:
    """Read watchdog-published health %. If KV unreachable (Supabase 522 from
    Kam2 happens often), do a quick LIVE probe to one keyless endpoint as
    fallback. Returns 50 (assume healthy) if any keyless answers, 0 if all
    fail."""
    try:
        from axentx_shared import kv_get
        h = kv_get("llm.providers.health") or {}
        if isinstance(h, dict) and h.get("v"): h = h["v"]
        if isinstance(h, dict) and "working_pct" in h:
            return int(h["working_pct"])
    except Exception:
        pass
    # Fallback: live probe MULTIPLE keyless endpoints. ANY reachable = healthy.
    import urllib.request, urllib.error, json as _json
    PROBES = [
        ("https://text.pollinations.ai/openai", "openai-fast"),
        ("https://api.llm7.io/v1/chat/completions", "gpt-oss-20b"),
        ("https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
         "Llama-3.1-8B-Instruct"),
        ("https://g4f.space/api/groq/openai/v1/chat/completions",
         "llama-3.3-70b-versatile"),
    ]
    reachable = 0
    for url, model in PROBES:
        try:
            body = _json.dumps({"model": model,
                                "messages": [{"role": "user", "content": "ok"}],
                                "max_tokens": 3}).encode()
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=6) as r:
                if r.status == 200:
                    reachable += 1
        except urllib.error.HTTPError as e:
            # 429 = rate-limited but reachable (will recover)
            # 403 = blocked — count as unreachable
            if e.code == 429:
                reachable += 1
        except Exception:
            pass
    # 0 reachable → 0%, 1 → 25%, 2 → 50%, 3 → 75%, 4 → 100%
    return reachable * 25


def _recent_pain_keywords() -> list[str]:
    """Mine last 24h shared_memory for pain-themed entries → keywords
    pointing at gaps in our current portfolio."""
    if not (SB_URL and SB_KEY):
        return []
    cutoff_iso = (datetime.datetime.utcnow()
                  - datetime.timedelta(hours=24)).isoformat()
    try:
        qs = urllib.parse.urlencode({
            "created_at": f"gte.{cutoff_iso}",
            "or": ("(kind.eq.fix,kind.eq.auth-fail,kind.eq.env-drift,"
                   "kind.eq.heal-stale-agent,kind.eq.snapshot)"),
            "select": "title,kind",
            "order": "created_at.desc",
            "limit": "60",
        })
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/shared_memory?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read())
        import re
        bag: dict[str, int] = {}
        for x in rows:
            for w in re.findall(r"[a-z]{4,}",
                                (x.get("title", "") or "").lower()):
                bag[w] = bag.get(w, 0) + 1
        return sorted(bag, key=lambda k: -bag[k])[:20]
    except Exception:
        return []


SYNTH_SYSTEM = (
    "You are axentx's Chief New-Ventures Strategist with a TH-blue-ocean "
    "lens. Your job: propose ONE bold, CREATIVE new product hypothesis the "
    "team should ship. Bias toward products that EXIST abroad but DON'T "
    "exist (or have <3 weak competitors) in Thailand — these are the easiest "
    "wins. Reward creativity HEAVILY: avoid me-too clones; prefer unusual "
    "angles, niche audiences, or local-context hacks. Output STRICT JSON:\n"
    "{\n"
    '  "domain": "short slug e.g. \\"th-pos-streetfood\\"",\n'
    '  "product_one_liner": "1-sentence hypothesis (unique angle)",\n'
    '  "creativity_score": "0-10, 10=novel angle no one has tried",\n'
    '  "audience": "specific buyer persona — be concrete (e.g. \\"Bangkok 7-Eleven franchisees\\")",\n'
    '  "thai_market": {\n'
    '     "tam_thb": "approx Thai TAM in THB billions (or \\"unknown — give qualitative range\\")",\n'
    '     "sam_thb": "Thai SAM in THB millions (servable subset)",\n'
    '     "som_thb": "Thai SOM in THB millions (realistically capturable yr1-3)",\n'
    '     "competitors_th": ["3 short names — Thai/SEA companies if any (use [] if none)"],\n'
    '     "competitor_strength": "none|weak|strong|dominated",\n'
    '     "growth_signal": "ขยายตัว|อิ่มตัว|ยังไม่เริ่ม",\n'
    '     "thai_specific_advantage": "1 phrase — what about Thailand makes this work (regulation, language, behavior, infra)"\n'
    '  },\n'
    '  "global_market": {\n'
    '     "tam_usd": "global TAM USD billions",\n'
    '     "sam_usd": "global SAM USD millions for our wedge",\n'
    '     "som_usd": "global SOM USD millions yr1-3",\n'
    '     "competitors_global": ["3 names with their wedge in 1 phrase each"],\n'
    '     "winner_signal": "fragmented|consolidating|monopolized"\n'
    '  },\n'
    '  "blue_ocean_signal": "TH-only|TH-first|global-niche|crowded — "\n'
    '                       "TH-only=exists abroad zero TH; TH-first=we lead Thailand; "\n'
    '                       "global-niche=tiny but defensible; crowded=skip",\n'
    '  "wedge": "the 1 unfair advantage axentx has (technical/local/data)",\n'
    '  "monetization": "subscription|usage|enterprise|marketplace|donation|none",\n'
    '  "monetization_signal": "low|medium|high",\n'
    '  "pricing_tier": "$X-Y/user/mo or THB equivalent or null",\n'
    '  "verdict": "OPEN-SOURCE|PAID-SAAS|SKIP",\n'
    '  "rationale": "2-3 sentence reasoning"\n'
    "}\n\n"
    "Strict guidance — read carefully:\n"
    "- creativity_score >= 7 REQUIRED — no boring copycats. If <7, try a "
    "different angle.\n"
    "- PROPOSE STRONGLY when blue_ocean_signal=TH-only OR TH-first AND "
    "competitor_strength in (none, weak). This is our highest-leverage zone.\n"
    "- PAID-SAAS when monetization_signal>=medium AND (TH SOM ≥ 50M THB OR "
    "global SOM ≥ $5M).\n"
    "- OPEN-SOURCE when real dev pain + no buyer yet + repo could earn "
    "stars (community moat for future paid layer).\n"
    "- SKIP only when monetization=none AND OS would outpace AND no Thai "
    "edge.\n"
    "- BIAS toward Thai-context categories: street food POS, motorbike-taxi "
    "tooling, Thai-language NLP, LINE OA automation, condo property mgmt, "
    "tourism micro-tools, Thai-tax/finance helpers, agriculture (rice/durian/"
    "rubber), B2B Thai-SME, government/BOI tooling, halal/Buddhist-temple "
    "specifics, K-pop/anime/manga subcultures inside TH.\n"
    "- Also surface global-niche ideas if creativity_score≥9 (universal pain "
    "but no one has tried this exact angle).\n"
)



def _trending_keywords() -> list[str]:
    """Read latest rising trends from trend-forecaster (via shared_kv).
    Returns up to 8 hot keywords. Falls back to [] on error."""
    try:
        from axentx_shared import kv_get
        v = kv_get("trend.rising_keywords")
        if isinstance(v, dict) and v.get("v"):
            v = v["v"]
        if isinstance(v, list):
            return [str(x)[:80] for x in v[:8]]
    except Exception:
        pass
    return []


def synthesize_one(domain_hint: str, recent_keywords: list[str]) -> dict | None:
    portfolio_block = get_portfolio_block()
    prompt = (
        f"# Existing axentx portfolio (DO NOT duplicate)\n{portfolio_block}\n\n"
        f"# Domain hint to explore\n{domain_hint}\n\n"
        f"# Recent pain keywords (from agent experiences last 24h)\n"
        f"{', '.join(recent_keywords[:15]) or '(none)'}\n\n"
        f"# RISING TRENDS (last 3d, prefer one of these as domain → first-mover advantage)\n"
        f"{', '.join(_trending_keywords()) or '(no trends published yet)'}\n\n"
        f"# Task\nPropose ONE new product hypothesis in this domain (or "
        f"a related one if domain_hint is weak). Compare Thai market vs "
        f"global. STRICT JSON only."
    )
    try:
        system = attach_rag(SYNTH_SYSTEM, prompt[:1500], max_snippets=5,
                            header="## Past harvested pains + similar product hypotheses")
        out = call_llm(prompt, system=system,
                       max_tokens=900, timeout=40)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        v = json.loads(txt.strip())
        return v if isinstance(v, dict) else None
    except Exception as e:
        log("product-synth",
            f"  ✗ synth ({domain_hint[:30]}): "
            f"{type(e).__name__}: {str(e)[:80]}")
        return None


def push_to_bd(spec: dict) -> bool:
    """Insert as pipeline_item at stage=bd so bd verdict + pitch-gate apply."""
    domain = spec.get("domain", "?")[:60]
    fid = (f"20260504-product-synth-"
           f"{hashlib.md5(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:10]}")
    pain_one_liner = (
        f"[product-synth] {spec.get('product_one_liner', '')[:200]}")
    payload = {
        "id": fid,
        "stage": "bd",
        "project": "",
        "focus": "new-product",
        "history": [{
            "stage": "product-synth",
            "actor": "axentx-product-synthesizer",
            "output": json.dumps(spec, ensure_ascii=False)[:1000],
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {"text": json.dumps(spec, ensure_ascii=False)},
        "verdict": {
            "pain_one_liner": pain_one_liner,
            "audience": spec.get("audience", ""),
            "monetization_signal": spec.get("monetization_signal", "low"),
            "domain": domain,
            "evidence": spec.get("rationale", "")[:300],
        },
        # Source-extracted signals — bd's enriched_block path will use these
        "axentx_idea": spec.get("product_one_liner", ""),
        "monetization_signal": spec.get("monetization_signal", "low"),
        "audience": spec.get("audience", ""),
        "pricing_signal": spec.get("pricing_tier", ""),
        "authority_score": 0.85,   # high — synthesized with Thai/global lens
        "source": "product-synthesizer",
        "post": {
            "title": pain_one_liner[:200],
            "body": json.dumps(spec, ensure_ascii=False)[:1500],
            "source": "product-synthesizer",
        },
    }
    body = {
        "id": fid, "stage": "bd",
        "project": "", "focus": "new-product",
        "payload": payload,
    }
    try:
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        log("product-synth", f"  ✗ insert {fid}: {e}")
        # _fs_synth_fallback: write candidate to local research-queue so bd picks it up
        try:
            from pathlib import Path
            import json as _j
            fs_dir = Path("/opt/surrogate-1-harvest/state/swarm-shared/research-queue")
            fs_dir.mkdir(parents=True, exist_ok=True)
            local_item = {
                "id": fid, "stage": "research",
                "project": None, "focus": "synth",
                "trace_id": fid,
                "current": {"text": (
                    f"## product-synth candidate ({fid})\n\n"
                    f"{_j.dumps(payload, ensure_ascii=False, indent=2)[:2500]}"
                )},
                "created_at": datetime.datetime.utcnow().isoformat() + "Z",
                "history": [{"stage":"synth","actor":"product-synthesizer",
                             "output":"FS fallback (supabase down)",
                             "at":datetime.datetime.utcnow().isoformat()+"Z"}],
                "extra": payload if isinstance(payload, dict) else {},
            }
            (fs_dir / f"{fid}.json").write_text(_j.dumps(local_item, indent=2))
            log("product-synth", f"  ↳ FS fallback: {fid} → research-queue")
            return True
        except Exception as ee:
            log("product-synth", f"  ✗ FS fallback failed: {ee}")
            return False


# Default domains we look at when no hints emerge — broad axentx-adjacent
# spaces NOT covered by current portfolio.
DEFAULT_DOMAINS = [
    "k8s observability for Thai mid-market SaaS (10-100 nodes)",
    "AI agent eval / red-teaming as a managed service",
    "data warehouse cost split by team for finops",
    "self-hosted Auth/SSO drop-in for Thai SMEs (PDPA-aware)",
    "MLOps for finetuning + serving small models on free GPU pools",
    "Postgres migration assistant (managed schema + zero-downtime)",
    "API gateway for AI agents — rate-limit pooling across providers",
    "Customer-support ticket triage with Thai-language LLM tuned",
    "Cloud egress cost analyzer (cross-cloud, GCP/AWS/CF)",
    "Compliance docs auto-generator for Thai PDPA + SOC2",
]


def _bd_queue_depth() -> int:
    if not (SB_URL and SB_KEY):
        return -1
    try:
        qs = urllib.parse.urlencode({"stage": "eq.bd", "select": "id"})
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Prefer": "count=exact", "Range": "0-0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            cr = r.headers.get("Content-Range", "")
            return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


# Trigger thresholds — only synth when bd is hungry, not on a cron.
BD_QUEUE_TARGET = int(os.environ.get("BD_QUEUE_TARGET", "20"))
BD_QUEUE_HARD_CAP = int(os.environ.get("BD_QUEUE_HARD_CAP", "150"))


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("product-synth", "  ⤷ not leader — skip")
        return False

    # Event-driven: only fire when bd queue is hungry. No fixed cron.
    bd_depth = _bd_queue_depth()
    if bd_depth >= BD_QUEUE_TARGET:
        log("product-synth",
            f"  ⤷ bd queue {bd_depth} ≥ target {BD_QUEUE_TARGET} — wait")
        return False
    if bd_depth >= BD_QUEUE_HARD_CAP:
        log("product-synth",
            f"  ⛔ bd queue {bd_depth} hit hard cap — skip")
        return False

    health = _llm_health_pct()
    if health < 15:
        log("product-synth", f"  ⤷ LLM {health}% < 15% — skip cycle")
        return False

    keywords = _recent_pain_keywords()
    log("product-synth",
        f"▸ synth cycle (LLM {health}%) — recent kw: "
        f"{', '.join(keywords[:6]) or 'none'}")

    # Mix recent-keyword domain + 1-2 default domains for diversity
    domains: list[str] = []
    if keywords:
        kw_domain = "any of these recent agent-pain themes: " + ", ".join(
            keywords[:8])
        domains.append(kw_domain)
    # Always include 2 default domains — round-robin via cycle # via timestamp
    import time as _t
    rotation = int(_t.time() // 1800) % len(DEFAULT_DOMAINS)
    domains.append(DEFAULT_DOMAINS[rotation])
    if len(domains) < MAX_NEW_PER_CYCLE:
        domains.append(DEFAULT_DOMAINS[(rotation + 3) % len(DEFAULT_DOMAINS)])

    pushed = 0
    for d in domains[:MAX_NEW_PER_CYCLE]:
        spec = synthesize_one(d, keywords)
        if not spec:
            continue
        verdict = (spec.get("verdict") or "").upper()
        if verdict == "SKIP":
            log("product-synth",
                f"  ⊘ {spec.get('domain','?')}: LLM verdict=SKIP "
                f"({(spec.get('rationale') or '')[:80]})")
            continue
        if push_to_bd(spec):
            pushed += 1
            log("product-synth",
                f"  ✓ pushed to bd: {spec.get('domain','?')[:40]} "
                f"({verdict}, mon={spec.get('monetization_signal')}, "
                f"th={spec.get('thai_market', {}).get('size_signal', '?')}, "
                f"global={spec.get('global_market', {}).get('size_signal', '?')})")
            try:
                from axentx_shared import memory_log
                memory_log("product-synth", "synthesized-product",
                           f"{spec.get('domain', '?')[:50]}: "
                           f"{spec.get('product_one_liner', '')[:120]}",
                           body=json.dumps(spec, ensure_ascii=False)[:1500],
                           tags=["product-synth", verdict.lower(), HOST])
            except Exception:
                pass

    log("product-synth",
        f"  ✓ pushed {pushed} new-product candidates to bd")
    return False


if __name__ == "__main__":
    daemon_loop("product-synth", POLL_SEC, cycle)
