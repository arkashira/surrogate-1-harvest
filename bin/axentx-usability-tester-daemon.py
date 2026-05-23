#!/usr/bin/env python3
"""axentx usability-tester — runs heuristic usability tests on each product's
current UX/UI output, reports issues, routes findings to ux-daemon for
redesign so ux is never idle.

User feedback 2026-05-04:
  > 'ต้องไปทำ usability testing มาก่อนด้วย ถ้าไม่ดีไม่เข้าใจ ผิด ก็ต้อง
  >  ไปออกแบบมาใหม่ ux/ui agent'

Cycle (event-driven, 90s tick):
  - Trigger: ux queue depth < 5 OR product has no recent ux-test in last 24h
  - For each candidate product:
      1. Read latest UX artifacts: /opt/axentx/<slug>/business/customer-journey.md
         + /opt/axentx/<slug>/business/user-stories.md + frontend src/ tree
      2. LLM heuristic eval (Nielsen 10) — score each + flag issues
      3. If issues found → push to ux-queue with "redesign with feedback X"
      4. If clean → memory_log "ux-test-passed"
  - Audit: every test run → shared_memory("usability-tester","test-result",...)
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
from axentx_pipeline import (log, daemon_loop, call_llm,  # noqa: E402
                             get_portfolio, get_portfolio_block)

POLL_SEC = int(os.environ.get("USABILITY_TESTER_POLL_SEC", "90"))
HOST = socket.gethostname()
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
UX_QUEUE_TARGET = int(os.environ.get("UX_QUEUE_TARGET", "5"))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _ux_queue_depth() -> int:
    if not (SB_URL and SB_KEY):
        return -1
    try:
        qs = urllib.parse.urlencode({"stage": "eq.ux", "select": "id"})
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


def _read(p: Path, n: int = 1500) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:n]
    except Exception:
        return ""


def _last_test_age_h(slug: str) -> float:
    try:
        from axentx_shared import kv_get
        v = kv_get(f"usability-test.{slug}") or {}
        if isinstance(v, dict) and v.get("v"): v = v["v"]
        if not isinstance(v, dict) or not v.get("ts"): return 99.0
        ts = datetime.datetime.fromisoformat(v["ts"].replace("Z", "+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return 99.0


USABILITY_SYSTEM = (
    "You are a senior UX researcher applying Nielsen's 10 heuristics + a "
    "Thai-language readability check (where relevant) to a product's "
    "current customer journey + user stories + frontend code-tree. Output "
    "STRICT JSON:\n"
    "{\n"
    '  "score_overall": 1-10,\n'
    '  "issues": [\n'
    '    {"heuristic": "<which Nielsen rule or other>",\n'
    '     "severity": "low|med|high|critical",\n'
    '     "where": "<file/journey-step>",\n'
    '     "fix_hint": "1-line concrete redesign suggestion"}\n'
    '  ],\n'
    '  "verdict": "PASS|REDESIGN_NEEDED",\n'
    '  "redesign_brief": "<if REDESIGN_NEEDED: 2-sentence brief for ux to act on>"\n'
    "}\n"
    "Heuristics to apply: visibility of system status, match to real "
    "world, user control + freedom, consistency + standards, error "
    "prevention, recognition vs recall, flexibility + efficiency, "
    "minimalist design, error recovery, help + docs. Bias toward action "
    "— if anything is unclear or could trip a Thai-speaking user, flag.")


def test_product(slug: str, repo: Path) -> dict | None:
    biz = repo / "business"
    journey = _read(biz / "customer-journey.md", 2000)
    stories = _read(biz / "user-stories.md", 2000)
    # Frontend tree summary
    fe_summary = ""
    for fe_root in (repo / "frontend", repo / "src", repo / "web"):
        if fe_root.exists() and fe_root.is_dir():
            try:
                files = sorted(
                    [str(p.relative_to(repo))
                     for p in fe_root.rglob("*")
                     if p.is_file() and p.suffix in (".tsx", ".jsx", ".vue", ".html")
                     and not any(x in str(p) for x in (".git", "node_modules"))])[:25]
                fe_summary = f"{fe_root.name}/ UI files:\n  " + "\n  ".join(files)
            except Exception:
                pass
            break
    if not (journey or stories or fe_summary):
        return None
    prompt = (
        f"# Product: {slug}\n\n"
        f"## Customer journey\n{journey or '(none)'}\n\n"
        f"## User stories\n{stories or '(none)'}\n\n"
        f"## Frontend tree\n{fe_summary or '(none)'}\n\n"
        f"Apply Nielsen 10 + Thai readability check. STRICT JSON only.")
    try:
        out = call_llm(prompt, system=USABILITY_SYSTEM,
                       max_tokens=900, timeout=40)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        return json.loads(txt.strip())
    except Exception as e:
        log("usability-tester",
            f"  ✗ {slug}: {type(e).__name__}: {str(e)[:80]}")
        return None


def push_to_ux(slug: str, brief: str, issues: list) -> bool:
    fid = f"20260504-uxtest-{slug}-{hashlib.md5(brief.encode()).hexdigest()[:10]}"
    issues_md = "\n".join(
        f"- [{i.get('severity', '?')}] {i.get('heuristic', '?')}: "
        f"{i.get('fix_hint', '')[:160]}"
        for i in issues[:8])
    body_text = (f"Usability test FAILED for {slug}.\n\n"
                 f"Brief: {brief}\n\nIssues found:\n{issues_md}")
    payload = {
        "id": fid, "stage": "ux", "project": slug,
        "focus": "usability-redesign",
        "history": [{
            "stage": "usability-tester",
            "actor": "axentx-usability-tester",
            "output": body_text[:1500],
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {"text": body_text},
        "auto_test_failed": True,
        "issues": issues,
    }
    body = {"id": fid, "stage": "ux", "project": slug,
            "focus": "usability-redesign", "payload": payload}
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
    except Exception:
        return False


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("usability-tester", "  ⤷ not leader — skip")
        return False
    if not PROJECTS_ROOT.exists():
        return False

    # Trigger: ux queue under target OR any product not tested in 24h
    ux_depth = _ux_queue_depth()
    if ux_depth >= UX_QUEUE_TARGET:
        log("usability-tester",
            f"  ⤷ ux queue {ux_depth} ≥ {UX_QUEUE_TARGET} — wait")
        return False

    portfolio = get_portfolio()
    if not portfolio:
        return False

    cutoff_h = 24
    candidates = []
    for slug in portfolio:
        if slug in {"arkship", "cost-radar"}:   # legacy/archived
            continue
        repo = PROJECTS_ROOT / slug
        if not repo.exists() or not (repo / ".git").exists():
            continue
        if _last_test_age_h(slug) > cutoff_h:
            candidates.append(slug)

    if not candidates:
        log("usability-tester",
            "  ✓ all products tested in last 24h — skip cycle")
        return False

    log("usability-tester",
        f"▸ ux_depth={ux_depth}, testing {min(3, len(candidates))} products")
    pushed = 0
    for slug in candidates[:3]:
        repo = PROJECTS_ROOT / slug
        result = test_product(slug, repo)
        if not result:
            continue
        verdict = (result.get("verdict") or "").upper()
        score = result.get("score_overall", 0)
        issues = result.get("issues", [])
        try:
            from axentx_shared import kv_set, memory_log
            kv_set(f"usability-test.{slug}", {
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "score": score, "verdict": verdict,
                "n_issues": len(issues),
            })
            memory_log("usability-tester", "test-result",
                       f"{slug}: score={score} verdict={verdict} "
                       f"issues={len(issues)}",
                       body=json.dumps(result, ensure_ascii=False)[:1500],
                       tags=["usability-tester", slug, verdict.lower()])
        except Exception:
            pass
        log("usability-tester",
            f"  ✓ {slug}: score={score} {verdict} ({len(issues)} issues)")
        if verdict == "REDESIGN_NEEDED" and issues:
            brief = result.get("redesign_brief", "")[:300]
            if push_to_ux(slug, brief, issues):
                pushed += 1
    log("usability-tester",
        f"  ✓ pushed {pushed} redesign-tasks to ux-queue")
    return False


if __name__ == "__main__":
    daemon_loop("usability-tester", POLL_SEC, cycle)
