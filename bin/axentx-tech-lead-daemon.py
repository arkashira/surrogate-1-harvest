#!/usr/bin/env python3
"""axentx tech-lead — writes RFCs for cross-cutting changes + breaks PRD
epics into concrete dev tickets with files[] + acceptance[].

Fills the "Tech Lead" gap identified in modern SaaS SDLC research:
- PMs write WHY (PRD/1-pager) → consumed by Tech Lead
- Tech Lead writes HOW: RFC (if cross-cutting) → ADR (decision logged)
- Tech Lead breaks epics → tickets → distributes to FE/BE/Mobile/DevOps devs
- Anyone in chain can punt back upstream if input is unclear

User feedback 2026-05-04:
  > 'ห้ามมี agent ไหนไม่มีงาน. ปัญหาคือร้อย flow ผิด'

Pipeline slot — sits between prd and dev:
  research → bd → ... → architect → ux → prd → ★ tech-lead ★ → dev

Cycle (event-driven, 60s tick):
  - Trigger: prd queue → tech-lead-pending OR dev queue < 30 (TL feeds dev)
  - For each PRD/spec: parse epics → for each epic:
      a. Decide: needs RFC? (cross-cutting / new dep / arch-impact) → write RFC
      b. Break into 3-8 dev tickets (frontend/backend/infra/test) parallel-able
      c. Each ticket has: title, files_likely[], acceptance[], complexity, dep_on
      d. Push tickets to dev queue WITH PARENT REFERENCE so we track delivery
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
import signal
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, daemon_loop, call_llm,  # noqa: E402
                             pick_oldest, advance, fail, get_role_budget,
                             get_portfolio_block)

POLL_SEC = int(os.environ.get("TECH_LEAD_POLL_SEC", "60"))
TL_BUDGET = get_role_budget("tech_lead", 2000)
HOST = socket.gethostname()
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


TL_SYSTEM = (
    "You are a senior Tech Lead at an axentx product squad. You receive "
    "a PRD/spec and produce: (1) optional RFC if the change is cross-"
    "cutting/new-dep/arch-impact (2) a CONCRETE ticket list that can be "
    "fanned out to FE/BE/DevOps/QA devs IN PARALLEL.\n\n"
    "Output STRICT JSON:\n"
    "{\n"
    '  "needs_rfc": true|false,\n'
    '  "rfc": {  /* only if needs_rfc */\n'
    '     "title": "...",\n'
    '     "decision_topic": "...",\n'
    '     "options": ["A: ...", "B: ...", "C: ..."],\n'
    '     "recommendation": "...",\n'
    '     "trade_offs": "1-3 sentences"\n'
    '  },\n'
    '  "tickets": [  /* 3-8 tickets, ALL parallel-able */\n'
    '    {\n'
    '       "title": "imperative — short",\n'
    '       "track": "frontend|backend|infra|test|docs|migration",\n'
    '       "files_likely": ["src/...", "test/..."],\n'
    '       "acceptance": ["concrete checkable criteria"],\n'
    '       "complexity": "S|M|L",\n'
    '       "depends_on_ticket": "<title>" or null,\n'
    '       "dev_handoff_brief": "1-paragraph spec dev can ship from"\n'
    '    }\n'
    '  ]\n'
    "}\n\n"
    "Rules:\n"
    "- Bias toward MORE small tickets over fewer large ones.\n"
    "- Tickets MUST be parallel-able unless explicitly depends_on_ticket.\n"
    "- Each dev_handoff_brief MUST be concrete enough that a dev can write "
    "code without going back to ask. Include exact file paths, function "
    "signatures if applicable.\n"
    "- needs_rfc=true ONLY when (a) introduces new external dep, "
    "(b) cross-cutting (touches >2 modules), or (c) reverses prior ADR.")


def push_ticket_to_dev(ticket: dict, parent_id: str, slug: str,
                       stack: dict | None = None) -> bool:
    track = ticket.get("track", "general")
    title = ticket.get("title", "")[:120]
    fid = (f"20260504-tl-{slug}-{track}-"
           f"{hashlib.md5((parent_id + title).encode()).hexdigest()[:10]}")
    stack_block = ""
    if stack:
        stack_block = (
            f"\n## Locked tech stack (MUST conform)\n"
            f"- Primary language: {stack.get('language_primary', '?')}\n"
            f"- Backend framework: {stack.get('backend_framework', 'n/a')}\n"
            f"- Frontend framework: {stack.get('frontend_framework', 'n/a')}\n"
            f"- Key libs: {', '.join(stack.get('key_libs', []))}\n"
            f"DEV: do NOT introduce other languages/frameworks. If you must,\n"
            f"emit a clarify block instead of writing diverging code.\n")
    brief = (
        f"# Ticket: {title}\n\n"
        f"Track: {track}\n"
        f"Complexity: {ticket.get('complexity', 'M')}\n"
        f"Files likely: {', '.join(ticket.get('files_likely', []))}\n"
        f"{stack_block}\n"
        f"## Acceptance criteria\n"
        + "\n".join(f"- {a}" for a in ticket.get("acceptance", []))
        + f"\n\n## Dev handoff\n{ticket.get('dev_handoff_brief', '')[:1500]}\n\n"
        f"Parent PRD: {parent_id}"
    )
    payload = {
        "id": fid, "stage": "dev", "project": slug,
        "focus": f"tl-{track}",
        "history": [{
            "stage": "tech-lead", "actor": "axentx-tech-lead",
            "output": brief[:1200],
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {"text": brief},
        "ticket": ticket,
        "parent_prd_id": parent_id,
        "track": track,
    }
    body = {"id": fid, "stage": "dev", "project": slug,
            "focus": f"tl-{track}", "payload": payload}
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


def write_rfc_to_repo(slug: str, rfc: dict) -> bool:
    """Save RFC.md to /opt/axentx/<slug>/decisions/RFC-YYYYMMDD-<title>.md"""
    repo = PROJECTS_ROOT / slug
    if not repo.exists():
        return False
    decisions = repo / "decisions"
    decisions.mkdir(exist_ok=True)
    title_slug = re.sub(r'[^a-z0-9-]', '-',
                        rfc.get("title", "rfc").lower())[:40]
    fname = (f"RFC-{datetime.datetime.utcnow().strftime('%Y%m%d')}-"
             f"{title_slug}.md")
    md = (
        f"# RFC: {rfc.get('title', '?')}\n\n"
        f"Status: proposed\n\n"
        f"## Decision topic\n{rfc.get('decision_topic', '')}\n\n"
        f"## Options\n"
        + "\n".join(f"{i+1}. {o}" for i, o in enumerate(rfc.get("options", [])))
        + f"\n\n## Recommendation\n{rfc.get('recommendation', '')}\n\n"
        f"## Trade-offs\n{rfc.get('trade_offs', '')}\n\n"
        f"---\nWritten by axentx-tech-lead-daemon @ {HOST} "
        f"on {datetime.datetime.utcnow().isoformat()}Z\n"
    )
    try:
        (decisions / fname).write_text(md)
        return True
    except Exception:
        return False


TECH_STACK_SYSTEM = (
    "You are a Tech Lead choosing the canonical tech stack for a brand-"
    "new project. Once you decide, the entire project MUST stick to this "
    "stack — no language mixing, no framework drift. Output STRICT JSON:\n"
    "{\n"
    '  "language_primary": "python|typescript|go|rust|java|kotlin",\n'
    '  "frontend_framework": "react|vue|svelte|none|n/a",\n'
    '  "backend_framework": "fastapi|express|nest|gin|fiber|spring|axum|n/a",\n'
    '  "db": "postgres|sqlite|none|...",\n'
    '  "key_libs": ["3-6 libs core to the stack"],\n'
    '  "deploy_target": "docker|vercel|aws-lambda|cloudflare-workers|...",\n'
    '  "rationale": "1-2 sentences why this stack fits the product"\n'
    "}\n"
    "Pick ONE language for primary. Frontend can be different (e.g. ts+\n"
    "python backend) but each side picks ONE. Keep it modern + simple +\n"
    "matches existing axentx products' style when possible.")


def ensure_tech_stack_decision(slug: str, repo: Path,
                                project_truth: dict) -> dict | None:
    """Make sure /opt/axentx/<slug>/decisions/tech-stack.md exists. If
    missing, the Tech Lead decides + writes it. Future dev tickets MUST
    reference this stack — no mixing Java + Python in a Python VPN tool."""
    decisions = repo / "decisions"
    decisions.mkdir(exist_ok=True)
    stack_md = decisions / "tech-stack.md"
    if stack_md.exists() and stack_md.stat().st_size > 200:
        try:
            txt = stack_md.read_text()
            # Quick parse: pull "Primary language: X" line
            m = re.search(r"^- Primary language: (.+)$", txt, re.MULTILINE)
            if m:
                return {"language_primary": m.group(1).strip(),
                        "from_existing": True, "raw": txt[:600]}
        except Exception:
            pass
    # Decide stack via LLM using project-truth context
    truth_summary = (
        f"Project: {slug}\n"
        f"What it actually does: {project_truth.get('one_liner', '?')}\n"
        f"Description: {project_truth.get('what_it_actually_is', '')[:500]}\n"
        f"Existing tech_stack tags from indexer: "
        f"{project_truth.get('tech_stack', [])}\n"
        f"Audience: {project_truth.get('audience_actual', '?')}"
    )
    try:
        out = call_llm(truth_summary
                       + "\n\nDecide the canonical tech stack. STRICT JSON.",
                       system=TECH_STACK_SYSTEM,
                       max_tokens=400, timeout=30)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        stack = json.loads(txt.strip())
    except Exception as e:
        log("tech-lead",
            f"  ✗ stack decision for {slug}: {type(e).__name__}: {str(e)[:80]}")
        return None
    md = (
        f"# Tech Stack — {slug}\n\n"
        f"Status: locked — all dev work MUST use this stack.\n"
        f"Decided: {datetime.datetime.utcnow().isoformat()}Z by "
        f"axentx-tech-lead-daemon@{HOST}\n\n"
        f"## The stack\n"
        f"- Primary language: {stack.get('language_primary', '?')}\n"
        f"- Frontend framework: {stack.get('frontend_framework', 'n/a')}\n"
        f"- Backend framework: {stack.get('backend_framework', 'n/a')}\n"
        f"- Database: {stack.get('db', 'n/a')}\n"
        f"- Key libs: {', '.join(stack.get('key_libs', []))}\n"
        f"- Deploy target: {stack.get('deploy_target', 'n/a')}\n\n"
        f"## Why this stack\n{stack.get('rationale', '')}\n\n"
        f"## Rules\n"
        f"- Devs MUST NOT introduce a different primary language.\n"
        f"- New deps must fit the listed key_libs philosophy.\n"
        f"- Changing this stack requires a new RFC + ADR superseding this.\n"
    )
    try:
        stack_md.write_text(md)
        log("tech-lead",
            f"  📜 LOCKED tech-stack for {slug}: "
            f"{stack.get('language_primary')}/{stack.get('backend_framework')}")
        return stack
    except Exception:
        return None


def fetch_project_truth(slug: str) -> dict:
    """Read shared_knowledge['project-truth/<slug>'] populated by
    codebase-indexer. Falls back to portfolio description."""
    try:
        from axentx_shared import knowledge_get
        v = knowledge_get(f"project-truth/{slug}")
        if isinstance(v, dict):
            try:
                return json.loads(v.get("body", "{}"))
            except Exception:
                pass
    except Exception:
        pass
    return {}


def do_one_tl() -> bool:
    if _stop:
        return False
    picked = pick_oldest("tech-lead")
    if not picked:
        return False
    src_path, item = picked
    project = item.get("project") or item.get("target_project") or "?"
    spec_text = (item.get("current") or {}).get("text", "")[:8000]
    if not spec_text or len(spec_text) < 80:
        fail(item, src_path, "tech-lead", "spec too thin to break down")
        return True

    log("tech-lead",
        f"▸ {item['id'][:32]} → {project} (breaking down spec)")

    # ── Lock tech stack BEFORE breaking down (user feedback 2026-05-04:
    #    'มึงเขียน java ผสม python — tech lead ต้องวางก่อน') ──────────
    repo = PROJECTS_ROOT / project
    if repo.exists() and (repo / ".git").exists():
        truth = fetch_project_truth(project)
        stack = ensure_tech_stack_decision(project, repo, truth)
    else:
        stack = None
        log("tech-lead",
            f"  ⚠ {project}: repo not local, skipping stack-lock step")

    stack_constraint = ""
    if stack:
        stack_constraint = (
            f"\n\n# LOCKED TECH STACK — every ticket MUST conform\n"
            f"- Primary language: {stack.get('language_primary')}\n"
            f"- Frontend framework: {stack.get('frontend_framework', 'n/a')}\n"
            f"- Backend framework: {stack.get('backend_framework', 'n/a')}\n"
            f"- Key libs: {', '.join(stack.get('key_libs', []))}\n"
            f"- Deploy: {stack.get('deploy_target', 'n/a')}\n\n"
            f"DO NOT mix languages. DO NOT introduce a framework not "
            f"in this list. If a ticket would require diverging, set "
            f'`needs_rfc=true` and write the RFC.\n')
    prompt = (
        f"# Project: {project}\n"
        f"{stack_constraint}"
        f"\n# PRD / spec to break down\n{spec_text[:6000]}\n\n"
        f"Output STRICT JSON only — your tickets list will be fanned out "
        f"to dev daemons in parallel. Make tickets concrete, parallel-able, "
        f"with exact file paths matching the locked stack."
    )
    try:
        out = call_llm(prompt,
                       system=TL_SYSTEM + "\n\n" + get_portfolio_block(),
                       max_tokens=TL_BUDGET, timeout=60)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        plan = json.loads(txt.strip())
    except Exception as e:
        fail(item, src_path, "tech-lead",
             f"LLM/parse: {type(e).__name__}: {e}")
        return True

    rfc_written = False
    if plan.get("needs_rfc") and plan.get("rfc"):
        rfc_written = write_rfc_to_repo(project, plan["rfc"])
        log("tech-lead",
            f"  📜 RFC written for {project}: {plan['rfc'].get('title','')[:50]}")

    tickets = plan.get("tickets") or []
    pushed = 0
    for tk in tickets[:8]:
        if push_ticket_to_dev(tk, item["id"], project, stack=stack):
            pushed += 1

    log("tech-lead",
        f"  ✓ {project}: {pushed} tickets → dev (parallel) "
        f"{'+ RFC' if rfc_written else ''}")

    advance(item, src_path, "done", "tech-lead",
            f"broke into {pushed} tickets"
            + (f" + RFC" if rfc_written else ""))

    try:
        from axentx_shared import memory_log
        memory_log("tech-lead", "broken-down",
                   f"{project}: {pushed} tickets fanned out",
                   body=json.dumps({
                       "tracks": [t.get("track") for t in tickets],
                       "rfc": rfc_written,
                       "complexities": [t.get("complexity") for t in tickets],
                   }),
                   tags=["tech-lead", project, HOST])
    except Exception:
        pass
    return True


if __name__ == "__main__":
    daemon_loop("tech-lead", POLL_SEC, do_one_tl)
