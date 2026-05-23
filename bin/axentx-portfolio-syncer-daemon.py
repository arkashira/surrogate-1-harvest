#!/usr/bin/env python3
"""axentx portfolio-syncer — keeps bd-daemon's product portfolio
DYNAMIC. Every product spawned/cloned to /opt/axentx/<slug>/ gets
auto-added to shared_kv["bd.portfolio"], so bd's verdicts can EXTEND
into newly-spawned products instead of treating them as unknown.

Sources:
  1. Hardcoded 5 main (Costinel, vanguard, arkship, surrogate, workio)
  2. /opt/axentx/<slug>/business/business-model-canvas.md (1-line summary)
  3. /opt/axentx/<slug>/README.md fallback (first paragraph)

User directive 2026-05-04:
  > 'agent ทุกตัวต้องรู้ด้วยนะ ว่าปัจจุบัน มี product อะไรอยู่แล้วบ้าง
  >  เมื่อ bd ได้ idea ใหม่ ที่เป็น feature ที่ extend จาก product เดิม
  >  ได้ ก็จะเขียน spec เพิ่มให้ใหม่'

Stored in shared_kv["bd.portfolio"] as a {slug: description} dict, so
ANY agent can read it. bd-daemon reads it at verdict-time instead of
using its hardcoded list.
"""
from __future__ import annotations
import datetime
import os
import re
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("PORTFOLIO_SYNC_POLL_SEC", "1800"))   # 30 min
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


# Hardcoded base — these are the 5 main paid axentx products with
# stable descriptions. Spawned products auto-discover below.
BASE_PORTFOLIO = {
    "Costinel":  "AWS cost analytics + anomaly detection (SREs/finops; surprise bills, unused resources, forecasting)",
    "vanguard":  "Cloud security posture management / CSPM (compliance officers, solo devs SOC2-lite; misconfig, drift, audit)",
    "arkship":   "(legacy) — folded into airship",
    "airship":   "IaC + multi-cloud DevSecOps unified (devs shipping AWS+GCP+CF; deploy-once-target-many, env parity, replaces 6 vendors)",
    "workio":    "Workflow automation (Zapier for eng teams; glue GitHub/Slack/Jira/HF without scripts)",
    "surrogate": "Autonomous AI dev agent (devs want commits/reviews/tests/docs done while sleeping; cloud free tier)",
}


def extract_one_liner(repo: Path) -> str:
    """Extract a 1-line description from /business/BMC or README."""
    bmc = repo / "business" / "business-model-canvas.md"
    if bmc.exists():
        try:
            txt = bmc.read_text(encoding="utf-8", errors="replace")
            # Look for "Value Propositions" block — first 200 chars
            m = re.search(r"## Value Propositions\s*\n(.*?)(?:\n##|\Z)",
                          txt, re.DOTALL | re.IGNORECASE)
            if m:
                body = re.sub(r"\s+", " ", m.group(1)).strip()
                return body[:240]
        except Exception:
            pass
    # Fallback: README first paragraph
    rd = repo / "README.md"
    if rd.exists():
        try:
            txt = rd.read_text(encoding="utf-8", errors="replace")
            # Skip headings, find first non-empty paragraph
            paras = [p.strip() for p in txt.split("\n\n")]
            for p in paras:
                p = re.sub(r"^#+\s.*", "", p).strip()
                p = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", p)
                p = re.sub(r"\s+", " ", p).strip()
                if len(p) > 30:
                    return p[:240]
        except Exception:
            pass
    return ""


def scan_spawned_products() -> dict:
    """Walk /opt/axentx/* and collect product descriptions."""
    out = {}
    if not PROJECTS_ROOT.exists():
        return out
    for repo in PROJECTS_ROOT.iterdir():
        if not repo.is_dir() or not (repo / ".git").exists():
            continue
        slug = repo.name
        desc = extract_one_liner(repo)
        if desc:
            out[slug] = desc
    return out


def _gh_list_repos(org_or_user: str, token: str) -> list[dict]:
    """List repos under an org/user. Uses /user/repos for owners we own,
    or /users/<name>/repos as fallback."""
    import urllib.request
    repos = []
    for endpoint in (f"https://api.github.com/users/{org_or_user}/repos?per_page=100&type=all",
                     f"https://api.github.com/orgs/{org_or_user}/repos?per_page=100"):
        try:
            req = urllib.request.Request(
                endpoint,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json",
                         "User-Agent": "axentx-portfolio-syncer"})
            with urllib.request.urlopen(req, timeout=15) as r:
                import json as _json
                repos.extend(_json.loads(r.read()))
                break
        except Exception:
            continue
    return repos


def scan_github_owners() -> dict:
    """Canonical product list from GitHub. SoT > local fs because each VM
    only clones a subset. Filters to repos with description containing
    'axentx product' or that match known spawn pattern."""
    import os
    out = {}
    # Token + owner-pool from env
    primary = os.environ.get("GITHUB_TOKEN", "").strip()
    pool = os.environ.get("GITHUB_TOKEN_POOL", "").strip()
    pool_tokens = [t.strip() for t in pool.split(",") if t.strip()]
    token = primary or (pool_tokens[0] if pool_tokens else "")
    if not token:
        return out
    # Owners that host axentx products. Each token can list its own repos
    # but org listing needs admin scope, so we fall back per owner.
    owners = ["arkashira", "ashirapit", "ashirafuse", "axentx-tech",
              "arkship-ai", "luckyburster-lab", "midnightgts",
              "ifusefreedomza", "surrogate-1"]
    seen_slugs = set()
    for owner in owners:
        repos = _gh_list_repos(owner, token)
        for r in repos:
            slug = r.get("name", "")
            desc = r.get("description") or ""
            # Heuristic: only count repos that look like axentx products
            # (description contains 'axentx product' OR slug matches pattern
            # we use for spawned products: short, lowercase, hyphen-separated)
            if not slug or slug in seen_slugs:
                continue
            if "axentx product" in desc.lower() or slug.startswith("surrogate") or slug in {
                    "Costinel","vanguard","airship","arkship","workio","surrogate"}:
                seen_slugs.add(slug)
                out[slug] = desc.strip() or "(no description)"
    return out


def fetch_project_truth() -> dict:
    """Pull shared_kv['project-truth.all'] — the GROUND TRUTH per project,
    written by codebase-indexer-daemon after reading actual source code.
    User feedback: BMC excerpts were stale (e.g. workio described as
    'Zapier for eng teams' when actual code is LINE punch-in/payroll)."""
    try:
        from axentx_shared import kv_get
        v = kv_get("project-truth.all") or {}
        if isinstance(v, dict) and v.get("v"): v = v["v"]
        truths = (v.get("projects") or {}) if isinstance(v, dict) else {}
        out = {}
        if isinstance(truths, dict):
            for slug, tr in truths.items():
                if isinstance(tr, dict) and tr.get("one_liner"):
                    out[slug] = tr["one_liner"][:240]
        return out
    except Exception:
        return {}


def do_one():
    portfolio = dict(BASE_PORTFOLIO)
    # PRIORITY 1: project-truth (real codebase analysis by codebase-indexer)
    # PRIORITY 2: GitHub repo description (if has 'axentx product' tag)
    # PRIORITY 3: local fs BMC excerpt (most stale — last resort)
    truth = fetch_project_truth()
    gh = scan_github_owners()
    fs = scan_spawned_products()
    # Merge in reverse priority: fs first → gh → truth (truth wins)
    for slug, desc in fs.items():
        portfolio[slug] = desc
    for slug, desc in gh.items():
        portfolio[slug] = desc
    for slug, desc in truth.items():
        portfolio[slug] = desc   # truth ALWAYS wins
    log("portfolio-syncer",
        f"  base={len(BASE_PORTFOLIO)} + truth={len(truth)} + "
        f"gh={len(gh)} + fs={len(fs)} = portfolio total={len(portfolio)}")
    try:
        from axentx_shared import kv_set
        kv_set("bd.portfolio", {
            "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "products": portfolio,
            "n_products": len(portfolio),
        })
        log("portfolio-syncer",
            f"  ✓ shared_kv['bd.portfolio'] updated "
            f"({', '.join(sorted(portfolio.keys())[:8])}...)")
    except Exception as e:
        log("portfolio-syncer",
            f"  ⚠ kv_set failed: {type(e).__name__}: {str(e)[:80]}")
    return False   # sleep full POLL_SEC


if __name__ == "__main__":
    daemon_loop("portfolio-syncer", POLL_SEC, do_one)
