#!/usr/bin/env python3
"""axentx product-spawner — turns NEW-PRODUCT verdicts into real GitHub
repos so the rest of the chain can build something concrete.

Why this daemon exists:

  bd-daemon classifies each pain as EXTEND <existing> | NEW-PRODUCT | PASS.
  Before this daemon, NEW-PRODUCT items were routed straight to design,
  flowed through architect → business → marketing → ux → prd → dev →
  review → qa → commit, then commit-daemon failed with
    'FAILED: project repo missing: /opt/axentx/null'
  and the item was marked done. Audit @ 2026-05-03 found 568 such items
  silently dropped.

  This daemon closes the gap: it claims spawn-queue items, picks a clean
  product slug from the LLM's hypothesis sentence, creates the GitHub
  repo via REST API, clones it locally, then advances the item to design
  with target_project = <new-slug>. Architect/business/marketing/ux/prd
  etc. now produce artifacts targeted at a real repo, and commit-daemon
  has somewhere to push.

Stage flow (this daemon's slot is **spawn**):
  research → validator → bd → spawn → design → architect → business →
    marketing → ux → prd → dev → review → qa → commit
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, pick_oldest, advance,
                             fail, daemon_loop)

POLL_SEC = int(os.environ.get("SPAWNER_POLL_SEC", "30"))
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
GH_OWNER = os.environ.get("AXENTX_GH_OWNER", "arkashira")
GH_TOKEN = (os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("AXENTX_GH_PAT", ""))

# Audit log of products created — append-only, used by anyone asking
# "which products has the chain spawned and when?"
PRODUCTS_AUDIT = REPO_ROOT / "state" / "swarm-shared" / "products-spawned.jsonl"
PRODUCTS_AUDIT.parent.mkdir(parents=True, exist_ok=True)

# Reserved names — never spawn these. Existing products + system reserves.
RESERVED_NAMES = {
    "costinel", "vanguard", "airship", "workio", "surrogate", "surrogate-1",
    "surrogate-1-harvest", "surrogate-1-runner", "surrogate-1-state",
    "axentx", "axiomops", "hermes", "arkship",
    "main", "master", "head", "null", "none", "test", "demo", "tmp",
}

SLUG_SYSTEM = (
    "You name new products. Given a product hypothesis sentence, output "
    "exactly ONE clean product slug — no more, no less.\n\n"
    "Rules (strict):\n"
    "- 1 to 3 words, total length 4–24 chars\n"
    "- lowercase kebab-case (hyphen-separated)\n"
    "- ASCII letters and hyphens only — no digits, no symbols\n"
    "- evocative + specific to the domain (e.g. 'gdpr-guard', 'cost-radar', "
    "'tracewright', 'commit-mind')\n"
    "- NEVER include the words: ai, gpt, smart, pro, hub, suite, platform, "
    "service, app, tool, kit, manager, system\n"
    "- NEVER reuse existing axentx product names: costinel, vanguard, "
    "airship, workio, surrogate\n"
    "- output the slug ONLY, on a single line, no explanation, no quotes"
)


def _gh_api(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    if not GH_TOKEN:
        return 0, {"error": "no GH_TOKEN configured"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        method=method, data=data,
        headers={
            "Authorization": f"token {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "axentx-product-spawner",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:
            payload = {"error": str(e)}
        return e.code, payload
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def _normalize_slug(raw: str) -> str | None:
    """Sanitize LLM output into a valid product slug, or None if invalid."""
    if not raw:
        return None
    s = raw.strip().lower()
    # take first line, drop quotes/code-fences/bullets
    s = s.splitlines()[0].strip().strip("`'\"-•* ")
    # squash whitespace + non-allowed chars to hyphens
    s = re.sub(r"[^a-z0-9-]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not (4 <= len(s) <= 30):
        return None
    if s in RESERVED_NAMES:
        return None
    # must be 1-3 words
    parts = s.split("-")
    if not (1 <= len(parts) <= 3):
        return None
    if any(len(p) < 2 for p in parts):
        return None
    return s


def _name_taken(slug: str) -> bool:
    """Already exists locally OR on GitHub?"""
    if (PROJECTS_ROOT / slug).exists():
        return True
    code, _ = _gh_api("GET", f"/repos/{GH_OWNER}/{slug}")
    return code == 200



def _spawn_quality_check(item: dict) -> tuple[bool, str]:
    """Return (passes, reason). Reject items missing real bd/market data.

    Prevents wasting GitHub repo slots + private-repo quota on items
    where upstream daemons (bd, market-research) failed silently.
    """
    bd = item.get("bd_verdict") or {}
    if not isinstance(bd, dict):
        return False, "bd_verdict not a dict"
    one_liner = (bd.get("new_product_one_liner") or
                 item.get("new_product_one_liner") or "")
    if not one_liner or len(one_liner) < 30:
        return False, f"hypothesis too short ({len(one_liner)} chars)"
    if "FAILED" in str(bd) or "LLM unavailable" in str(bd):
        return False, "bd_verdict marked FAILED"
    # Market data check — at least one of TAM/SAM should be non-zero
    mr = item.get("market_research") or {}
    if isinstance(mr, str):
        try:
            import json as _j
            mr = _j.loads(mr)
        except Exception:
            mr = {}
    has_market = any([
        mr.get("tam_global_usd", 0) or 0,
        mr.get("sam_global_usd", 0) or 0,
        mr.get("tam_thai_thb", 0) or 0,
        mr.get("sam_thai_thb", 0) or 0,
    ])
    if not has_market and mr:
        if (bd.get("monetization") and
                bd.get("monetization") != "none" and
                bd.get("monetization_signal") in ("medium", "high")):
            pass   # fall through to pitch_verdict check
        else:
            return False, "market_research all zero + no monetization signal"
    # 2026-05-05: also require pitch verdict to confirm competitor + will_pay
    pv = item.get("pitch_verdict") or {}
    panel = pv.get("panel") or []
    if isinstance(panel, list) and panel:
        n_invest_yes = sum(1 for p in panel if isinstance(p, dict) and p.get("would_invest_or_pay") is True)
        if n_invest_yes < 1:
            return False, "no persona said would_invest_or_pay=true"
    return True, "passed quality gate"


def pick_slug(hypothesis: str, max_attempts: int = 4) -> str | None:
    """Ask the LLM for a slug, retry with feedback if invalid/taken."""
    used: list[str] = []
    for attempt in range(max_attempts):
        prompt = (
            f"Hypothesis: {hypothesis}\n\n"
            + (f"Avoid (already used / invalid): {', '.join(used)}\n"
               if used else "")
            + "Output one slug only."
        )
        try:
            raw = call_llm(prompt, system=SLUG_SYSTEM, max_tokens=20)
        except Exception as e:
            log("spawner", f"  LLM slug attempt {attempt + 1} failed: {e}")
            continue
        slug = _normalize_slug(raw)
        if not slug:
            log("spawner", f"  attempt {attempt + 1}: invalid '{raw[:40]}'")
            continue
        if _name_taken(slug):
            log("spawner", f"  attempt {attempt + 1}: '{slug}' already taken")
            used.append(slug)
            continue
        return slug
    return None


def _resolve_owner() -> str:
    """The PAT might belong to a different user than AXENTX_GH_OWNER.
    Resolve the authenticated user once at startup so repo-creation lands
    where we can actually write. Verified 2026-05-03: PAT issued to
    ashirapit cannot POST to /orgs/arkashira/repos (404, arkashira is a
    user not org), nor /user/repos creates under arkashira (creates
    under the PAT-owner = ashirapit). Existing repos at github.com/
    arkashira/<x> are reachable via git protocol but 404 via API."""
    code, payload = _gh_api("GET", "/user")
    if code == 200 and payload.get("login"):
        return payload["login"]
    return GH_OWNER  # last-resort fallback


_AUTH_OWNER = None


def get_auth_owner() -> str:
    global _AUTH_OWNER
    if _AUTH_OWNER is None:
        _AUTH_OWNER = _resolve_owner()
        if _AUTH_OWNER != GH_OWNER:
            log("spawner",
                f"  ⓘ PAT-owner ({_AUTH_OWNER}) differs from "
                f"AXENTX_GH_OWNER ({GH_OWNER}). New repos will land at "
                f"github.com/{_AUTH_OWNER}/<slug>.")
    return _AUTH_OWNER


def create_repo(slug: str, hypothesis: str,
                output_mode: str = "paid-product") -> str | None:
    """Create the GitHub repo + clone it locally. Idempotent.

    Repo visibility per output_mode (added 2026-05-04, user directive):
      - "paid-product"  → PRIVATE (revenue-generating, keep code closed)
      - "extend-main"   → PRIVATE (axentx core IP)
      - "agent-tool"    → PRIVATE (internal tooling)
      - "open-source"   → PUBLIC (community, marketing-driver)

    Returns the actual owner login on success, None on failure."""
    description = f"axentx product · {hypothesis[:200]}"
    owner = get_auth_owner()
    private = output_mode != "open-source"

    # Always go via /user/repos — POSTing to /orgs/<owner>/repos requires
    # the authenticated user to be an org member with create-repo perms,
    # which fails for user-typed accounts.
    code, payload = _gh_api("POST", "/user/repos", {
        "name": slug, "description": description,
        "private": private, "auto_init": True,
    })
    if code not in (201, 422):  # 422 = already exists
        log("spawner",
            f"  ✗ gh repo create {slug} failed: HTTP {code} "
            f"{str(payload)[:160]}")
        return None
    if code == 422:
        log("spawner", f"  ↺ {slug} already on GitHub — cloning")

    repo_dir = PROJECTS_ROOT / slug
    if not repo_dir.exists():
        clone_url = (f"https://{owner}:{GH_TOKEN}@github.com/"
                     f"{owner}/{slug}.git")
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", clone_url, str(repo_dir)],
                check=True, capture_output=True, timeout=60,
            )
        except subprocess.CalledProcessError as e:
            log("spawner",
                f"  ✗ clone {slug} failed: {e.stderr.decode()[:160]}")
            return None
    return owner


def do_one() -> bool:
    picked = pick_oldest("spawn")
    if not picked:
        return False
    src_path, item = picked
    bd = item.get("bd_verdict") or {}
    verdict = (bd.get("verdict") or "").upper()

    if verdict != "NEW-PRODUCT":
        # Defensive: not for us. Pass through to design unchanged.
        log("spawner",
            f"  ⤷ {item['id'][:32]} verdict={verdict!r}, not NEW-PRODUCT — "
            "forwarding to design unchanged")
        advance(item, src_path, "design", "spawner",
                f"forward (verdict={verdict})")
        return True

    hypothesis = (bd.get("new_product_one_liner")
                  or item.get("current", {}).get("text", "")
                  or "")[:300]
    if not hypothesis.strip():
        fail(item, src_path, "spawner", "no new_product_one_liner")
        return True

    # ── Pitch gate (added 2026-05-04 after cost-radar bypass) ──────────────
    # User feedback: 'cost-radar ผ่าน pitch มาเหรอ' — answer was NO, because
    # pipeline order put pitch AFTER spawn. New rule: refuse to spawn unless
    # the bd verdict carries a pitch_verdict={GO|PIVOT|NO-GO}. Items without
    # pitch_verdict get rerouted to 'pitch' stage instead so the panel can
    # gate them BEFORE we burn GH org slots + business-synthesis cycles.
    pv_block = item.get("pitch_verdict")
    # 2026-05-05: REQUIRE pitch_verdict to exist (not None / not empty dict).
    # Was permissive before — spawner created cloud-lab/cloud-pilot with
    # pitch_verdict=None because spawn path bypassed pitch.
    if not pv_block or not isinstance(pv_block, dict) or not pv_block.get("verdict"):
        log("spawner",
            f"  ⛔ {item['id'][:32]} NO pitch_verdict at all — routing to pitch first")
        advance(item, src_path, "pitch", "spawner",
                json.dumps({"reason": "spawn-blocked: pitch verdict missing",
                            "hypothesis": (((item.get("bd_verdict") or {}).get("new_product_one_liner") or "")[:200])}))
        return True
    pitch_v = (pv_block.get("verdict") or "").upper()
    pivot_count = int(item.get("pivot_count", 0))
    # 2026-05-05: also allow PIVOT items with avg≥4 (decent panel score)
    pitch_avg = float(((item.get("pitch_verdict") or {}).get("avg_score") or 0))
    pivot_decent = (pitch_v == "PIVOT" and pivot_count < 3 and pitch_avg >= 4.0)
    if pitch_v not in ("GO", "PIVOT_APPROVED") and not pivot_decent:
        log("spawner",
            f"  ⛔ {item['id'][:32]} no pitch=GO yet (pitch_verdict={pitch_v or 'none'})"
            f" — routing to pitch first")
        advance(item, src_path, "pitch", "spawner",
                json.dumps({"reason": "spawn-blocked: pitch gate required",
                            "hypothesis": hypothesis[:200]}))
        return True

    log("spawner",
        f"▸ {item['id'][:32]}  hypothesis: {hypothesis[:60]}")

    slug = pick_slug(hypothesis)
    if not slug:
        fail(item, src_path, "spawner",
             "could not generate unique valid slug after retries")
        return True

    # output_mode set by bd-daemon: paid-product / open-source / extend-main
    output_mode = item.get("output_mode") or (
        item.get("bd_verdict", {}) or {}).get("output_mode") or "paid-product"
    # 2026-05-05: spawn-quality gate — reject items where bd or market data is failed/empty
    passes, reason = _spawn_quality_check(item)
    if not passes:
        log("spawner",
            f"  ⛔ {item['id'][:32]} REJECTED at spawn-quality-gate: {reason} — back to bd")
        # Send back to bd queue for retry (with retry counter)
        item["bd_retry_count"] = int(item.get("bd_retry_count", 0)) + 1
        if item["bd_retry_count"] >= 3:
            fail(item, src_path, "spawner",
                 f"spawn-quality-gate failed 3x: {reason}")
            return True
        advance(item, src_path, "bd", "spawner",
                f"quality-gate-fail: {reason} (retry {item['bd_retry_count']}/3)")
        return True

    owner = create_repo(slug, hypothesis, output_mode=output_mode)
    if owner is None:
        fail(item, src_path, "spawner", f"repo creation failed for {slug}")
        return True

    repo_url = f"https://github.com/{owner}/{slug}"
    log("spawner", f"  ✓ created {repo_url} (mode={output_mode}, "
                   f"private={output_mode != 'open-source'})")
    # Audit log
    with PRODUCTS_AUDIT.open("a") as f:
        f.write(json.dumps({
            "at": datetime.datetime.utcnow().isoformat() + "Z",
            "owner": owner,
            "slug": slug,
            "hypothesis": hypothesis,
            "trace_id": item.get("trace_id"),
            "item_id": item.get("id"),
            "url": repo_url,
        }, ensure_ascii=False) + "\n")

    item["target_project"] = slug
    item["project"] = slug
    item["repo_url"] = repo_url
    log("spawner", f"  ✓ spawned {owner}/{slug} — {repo_url}")
    # Route through business-synthesis BEFORE design so the new repo
    # gets BMC + marketing + tech spec + customer journey + dataflow +
    # user stories committed first. design-thinking + architect then
    # build on that foundation.
    advance(item, src_path, "business-synthesis", "spawner",
            json.dumps({"slug": slug, "owner": owner, "url": repo_url}))
    return True


if __name__ == "__main__":
    if not GH_TOKEN:
        log("spawner",
            "FATAL: GH_TOKEN/GITHUB_TOKEN/AXENTX_GH_PAT not set in env. "
            "Spawner cannot create repos. Set in /etc/surrogate-coordinator.env.")
        sys.exit(1)
    daemon_loop("spawner", POLL_SEC, do_one)
