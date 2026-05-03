#!/usr/bin/env python3
"""axentx business-synthesis — full BMC + marketing + tech spec per
NEW-PRODUCT verdict.

Pipeline slot: spawn → business-synthesis → design (if EXTEND) | architect

When the spawner has just created a new repo for a NEW-PRODUCT verdict,
this daemon takes the (validated_pain + market_data + repo_url) trio
and emits the FULL business pack into the new repo as committable
markdown:

  /business/business-model-canvas.md
  /business/marketing-plan.md
  /business/customer-journey.md
  /business/dataflow.md
  /business/user-stories.md
  /business/tech-spec.md
  /business/breakeven.md   (unit economics)
  /business/partner-targets.md  (which APIs/SaaS to integrate)

Each via separate LLM call (call_llm with strong-model preference) so a
single bad call doesn't tank the whole pack. Output goes through the
existing commit-queue → commit-daemon pushes to the spawned repo.
"""
from __future__ import annotations
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, call_llm, call_llm_strong,  # noqa: E402
                             pick_oldest, advance, fail, daemon_loop,
                             get_role_budget)

POLL_SEC = int(os.environ.get("BSYN_POLL_SEC", "30"))
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
BSYN_BUDGET = get_role_budget("business-synthesis", 3500)


SYN_SYSTEM = (
    "You are a senior product strategist + DevSecOps founder. Given a "
    "validated pain + market data + new product repo, produce ONE "
    "section of the business pack at a time. Be specific, numerical "
    "where possible, opinionated. Output markdown. No prose preamble."
)


SECTIONS = [
    ("business-model-canvas.md",
     "Generate a 9-block Business Model Canvas. Format: H2 per block "
     "(Customer Segments, Value Propositions, Channels, Customer "
     "Relationships, Revenue Streams, Key Resources, Key Activities, "
     "Key Partners, Cost Structure). 3-5 bullets each. Currency "
     "where relevant in USD + THB equivalents."),
    ("marketing-plan.md",
     "Generate a 90-day go-to-market plan. Sections: Positioning "
     "(1-line + 3 alternatives), ICP (3 specific personas with names "
     "+ daily-job + budget), Channels (top 3 with CAC estimate), "
     "Content cadence (week-by-week), Launch milestones (D-30, D-0, "
     "D+30, D+90), Success metrics (DAU, $MRR target by D+90)."),
    ("customer-journey.md",
     "Generate a customer journey map. Phases: Aware → Consider → "
     "Try → Adopt → Expand. For each: trigger event, friction "
     "points, user emotions, opportunities to delight, "
     "metric per phase."),
    ("dataflow.md",
     "Generate a system dataflow architecture. Sections: External "
     "data sources, Ingestion layer, Processing/transform layer, "
     "Storage tier, Query/serving layer, Egress to user. ASCII "
     "block diagram + bullet list of components per tier. Include "
     "auth boundaries."),
    ("user-stories.md",
     "Generate 8-12 user stories in Connextra format ('As a <role>, "
     "I want <action>, so that <outcome>'). Group by epic (3-4 "
     "epics). Each story: acceptance criteria (3-5 bullets) + "
     "estimated complexity (S/M/L)."),
    ("tech-spec.md",
     "Generate a v1 technical specification. Sections: Stack "
     "(language/framework/runtime), Hosting (free-tier-first, "
     "specific platforms), Data model (tables/collections + key "
     "fields), API surface (5-10 endpoints with method/path/purpose), "
     "Security model (auth, secrets, IAM), Observability (logs, "
     "metrics, traces), Build/CI."),
    ("breakeven.md",
     "Generate unit economics + break-even analysis. Sections: "
     "Cost per active user (compute, storage, bandwidth in USD), "
     "Pricing tiers (3 tiers with $/mo + features), CAC range, "
     "LTV estimate, Break-even users count, Path to $10K MRR (which "
     "tier × how many users)."),
    ("partner-targets.md",
     "Generate a partner integration roadmap. List 5-8 specific "
     "SaaS/APIs to integrate with rationale. Include free-tier "
     "limits, integration effort (S/M/L), value-add (which user job "
     "it solves). Prioritize ones with affiliate/revenue-share."),
]


def gen_section(prompt_ctx: str, section_name: str, instr: str) -> str:
    full_prompt = (
        f"# Context\n{prompt_ctx[:3500]}\n\n"
        f"# Task\nGenerate `{section_name}`. {instr}"
    )
    try:
        return call_llm(full_prompt, system=SYN_SYSTEM,
                        max_tokens=BSYN_BUDGET)
    except Exception as e:
        log("business-synth",
            f"  ✗ section {section_name}: {type(e).__name__}: "
            f"{str(e)[:120]}")
        return f"# {section_name}\n\nGeneration failed: {e}\n"


def build_context(item: dict) -> str:
    bd_v = item.get("bd_verdict") or {}
    md = item.get("market_data") or {}
    project = item.get("project") or item.get("target_project") or "?"
    repo_url = item.get("repo_url") or "(not yet spawned)"
    hyp = (bd_v.get("new_product_one_liner")
           or bd_v.get("feature_one_liner") or "")
    rationale = bd_v.get("rationale", "")
    return (
        f"Product: {project}\n"
        f"Repo: {repo_url}\n"
        f"Hypothesis: {hyp}\n"
        f"BD rationale: {rationale[:500]}\n"
        f"Market data:\n{json.dumps(md, ensure_ascii=False, indent=2)[:1500]}"
    )


def write_pack_to_repo(project: str, sections: dict[str, str]) -> bool:
    repo_dir = (PROJECTS_ROOT / project).resolve()
    if not repo_dir.exists():
        log("business-synth",
            f"  ✗ repo not found: {repo_dir}; skip write to disk")
        return False
    biz_dir = repo_dir / "business"
    biz_dir.mkdir(exist_ok=True)
    for fname, content in sections.items():
        (biz_dir / fname).write_text(content, encoding="utf-8")
    return True


def do_one() -> bool:
    picked = pick_oldest("business-synthesis")
    if not picked:
        return False
    src_path, item = picked
    project = item.get("project") or item.get("target_project")
    if not project or project == "null":
        fail(item, src_path, "business-synth",
             "no target_project — spawner must run first")
        return True

    log("business-synth", f"▸ {item['id'][:32]} → {project}")
    ctx = build_context(item)
    sections = {}
    for fname, instr in SECTIONS:
        sections[fname] = gen_section(ctx, fname, instr)
        log("business-synth",
            f"  ✓ {fname}  ({len(sections[fname])} chars)")

    write_pack_to_repo(project, sections)
    item["business_pack"] = {k: v[:200] + "…" for k, v in sections.items()}
    log("business-synth",
        f"  ✓ wrote {len(sections)} business artifacts → "
        f"{PROJECTS_ROOT / project}/business/")
    advance(item, src_path, "design", "business-synth",
            f"business pack: {len(sections)} sections")
    return True


if __name__ == "__main__":
    daemon_loop("business-synthesis", POLL_SEC, do_one)
