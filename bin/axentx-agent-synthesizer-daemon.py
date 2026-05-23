#!/usr/bin/env python3
"""axentx agent-synthesizer — meta-agent that observes the system's gaps
and PROPOSES new agents to build. Hourly cycle.

Inputs (read-only):
  - shared_memory recent (lessons/fixes from all hosts)
  - shared_knowledge (what we already know how to do)
  - pipeline queue depths (which stages back up)
  - failed/timeout patterns (what keeps breaking)

Output: writes proposed-agent specs to shared_knowledge under
slug="agent-proposal/<name>" — operator (or future autonomous spawn)
can spec → build → deploy.

Each proposal contains:
  - agent name + 1-line purpose
  - input source
  - LLM extraction strategy
  - emit target stage
  - estimated impact (which queue it would unblock)
  - estimated cost (LLM calls/day)
  - reference patterns from existing agents
"""
from __future__ import annotations
import datetime
import json
import os
import signal
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, call_llm_strong, call_llm,  # noqa: E402
                             daemon_loop, get_role_budget)

POLL_SEC = int(os.environ.get("AGENT_SYNTH_POLL_SEC", "3600"))
SYNTH_BUDGET = get_role_budget("agent-synthesizer", 1500)


_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


SYNTH_SYSTEM = (
    "You are a senior systems architect for axentx — an autonomous "
    "product-discovery + ship pipeline. You observe the system and "
    "propose NEW AGENTS to build. Each proposal must be concrete enough "
    "that a developer (or another LLM) can implement it from the spec "
    "alone. Reject vague suggestions like 'better LLM agent' — propose "
    "named agents with specific input sources, extraction logic, and "
    "emit targets."
)

SYNTH_PROMPT = """# Current system state

## Existing agents (don't propose duplicates)
{existing_agents}

## Recent pipeline gaps (queues backing up)
{queue_state}

## Recent lessons / failures (last 20)
{recent_memory}

## Knowledge gaps (topics in user requests with NO matching knowledge entry)
{knowledge_gaps}

# Your task

Propose 3-5 NEW agents that would meaningfully improve axentx. Each must:
- Be implementable in <300 lines of Python (the existing daemon template)
- Use only FREE-TIER APIs (no paid keys we don't have)
- Have a CONCRETE input source (RSS / public API / git repo / etc.)
- Emit to a specific pipeline stage (validator / pitch / design / dev / ...)

Output STRICT JSON array:

[
  {{
    "name": "axentx-<slug>-daemon",
    "purpose": "1-sentence what it does",
    "input_source": "specific URL / API / repo",
    "extraction_logic": "1-sentence how to parse",
    "emit_to_stage": "validator|pitch|design|...",
    "monetization_signal_default": "low|medium|high",
    "poll_seconds": 1800,
    "estimated_llm_calls_per_day": 50,
    "estimated_impact": "what this unblocks (1 sentence)",
    "reference_pattern": "axentx-<existing-daemon> if similar pattern exists",
    "rationale": "why now, why not later (1 sentence)"
  }}
]

Skip duplicates of existing agents. Prefer high-impact / low-cost first.
"""


def fetch_existing_agents() -> list[str]:
    bin_dir = REPO_ROOT / "bin"
    return sorted([
        p.stem for p in bin_dir.glob("axentx-*-daemon.py")
    ])


def fetch_queue_state() -> dict:
    """Pull current queue depths via Supabase REST."""
    url = os.environ.get("SUPABASE_URL", "")
    key = (os.environ.get("SUPABASE_SECRET_KEY")
           or os.environ.get("SUPABASE_SERVICE_KEY", ""))
    if not (url and key):
        return {}
    state = {}
    for stage in ("research", "validator", "market-research", "bd",
                  "pitch", "spawn", "business-synthesis",
                  "competitor-intel", "design", "architect", "ux", "prd",
                  "dev", "review", "qa", "commit", "mvp-validator"):
        try:
            qs = urllib.parse.urlencode({
                "stage": f"eq.{stage}",
                "select": "id",
            })
            req = urllib.request.Request(
                f"{url}/rest/v1/pipeline_items?{qs}",
                headers={
                    "apikey": key, "Authorization": f"Bearer {key}",
                    "Prefer": "count=exact", "Range": "0-0",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                cr = r.headers.get("Content-Range") or ""
                if "/" in cr:
                    state[stage] = int(cr.split("/")[-1])
        except Exception:
            pass
    return state


def fetch_recent_memory() -> list[dict]:
    try:
        from axentx_shared import memory_recent
        return memory_recent(kind="lesson", limit=20) + \
               memory_recent(kind="fix", limit=10)
    except Exception:
        return []


def fetch_knowledge_gaps() -> list[str]:
    """List slugs we know about — caller LLM identifies gaps from this list."""
    try:
        from axentx_shared import knowledge_search
        # Get a broad sample of slugs
        hits = knowledge_search("a", limit=50)
        return [h.get("slug", "") for h in hits if h.get("slug")]
    except Exception:
        return []


def do_one():
    if _stop:
        return False

    existing = fetch_existing_agents()
    queue = fetch_queue_state()
    memory = fetch_recent_memory()
    knowledge = fetch_knowledge_gaps()

    # Format queue state — flag stages with > 100 pending as "back-up"
    queue_lines = []
    for k, v in sorted(queue.items(), key=lambda x: -x[1]):
        flag = " ⚠ backed up" if v > 100 else ""
        queue_lines.append(f"  {k}: {v}{flag}")
    queue_state = "\n".join(queue_lines) or "(no queue data)"

    memory_lines = []
    for m in memory[:20]:
        memory_lines.append(
            f"  [{m.get('kind')}] {m.get('title','')[:80]} "
            f"({m.get('host','?')})")
    recent_memory = "\n".join(memory_lines) or "(no recent memory)"

    knowledge_summary = ", ".join(knowledge[:30]) or "(empty)"

    prompt = SYNTH_PROMPT.format(
        existing_agents="\n".join("  - " + a for a in existing[:60]),
        queue_state=queue_state,
        recent_memory=recent_memory,
        knowledge_gaps=knowledge_summary,
    )

    try:
        out = call_llm_strong(prompt, system=SYNTH_SYSTEM,
                              max_tokens=SYNTH_BUDGET, timeout=60)
    except Exception:
        try:
            out = call_llm(prompt, system=SYNTH_SYSTEM,
                           max_tokens=SYNTH_BUDGET, timeout=50)
        except Exception as e:
            log("agent-synth", f"  ✗ LLM: {type(e).__name__}: {str(e)[:120]}")
            return False

    txt = out.strip()
    if "```" in txt:
        seg = txt.split("```", 2)
        if len(seg) >= 2:
            txt = seg[1]
            if txt.startswith("json"):
                txt = txt[4:]
            txt = txt.strip()

    try:
        proposals = json.loads(txt)
    except Exception:
        log("agent-synth", f"  ✗ parse fail: {txt[:200]}")
        return False

    if not isinstance(proposals, list):
        log("agent-synth", "  ✗ expected JSON array")
        return False

    # Dedup against existing agents + write each as a knowledge entry
    saved = 0
    try:
        from axentx_shared import knowledge_set
        for p in proposals:
            if not isinstance(p, dict):
                continue
            name = p.get("name", "").lower().strip()
            if not name:
                continue
            if name in existing or name + "@" in existing:
                continue   # already exists
            slug = f"agent-proposal/{name.replace('axentx-', '').replace('-daemon','')}"
            body = json.dumps(p, indent=2, ensure_ascii=False)
            title = f"Proposed agent: {name}"
            if knowledge_set(slug, "agent-proposal", title, body, p):
                saved += 1
                log("agent-synth", f"  ✓ proposed {name}: {p.get('purpose','')[:80]}")
    except Exception as e:
        log("agent-synth",
            f"  ⚠ knowledge_set unavailable: {type(e).__name__}: {str(e)[:80]}")

    log("agent-synth",
        f"cycle: {len(proposals)} candidates, {saved} new proposals saved")
    return True


if __name__ == "__main__":
    daemon_loop("agent-synthesizer", POLL_SEC, do_one)
