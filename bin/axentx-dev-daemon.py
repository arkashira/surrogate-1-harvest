#!/usr/bin/env python3
"""axentx dev daemon — continuously generates dev tasks for the rotation
of axentx projects. Picks next (project, focus) pair every 5 min, calls
LLM with the dev role prompt, drops result into review-queue.

Replaces the cron-based axentx-unified job (every 15 min burst).
This is the producer of the work pipeline.
"""
from __future__ import annotations

import json
import os
import sys
import time
import datetime
import subprocess
from pathlib import Path

# import shared infra
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, QUEUES, log, call_llm, synthesize,
                             new_item, write_item, daemon_loop,
                             pick_oldest, advance, get_role_budget, get_portfolio_block,
                              get_portfolio,)

DEV_BUDGET = get_role_budget("dev", 2000)
DEV_REFINE_BUDGET = get_role_budget("dev_refine", 2500)

PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
ROTATION = ["Costinel", "vanguard", "airship", "workio", "surrogate-1"]
# axiomops removed 2026-05-02 — rebranded into airship (target shifted)
FOCUS_CYCLE = ["discovery", "design", "backend", "frontend", "quality", "ops"]


_GLOBAL_INDEX_PATHS = (
    REPO_ROOT / "data" / "memory" / "knowledge_index.md",
    Path.home() / ".claude" / "memory" / "knowledge_index.md",
)
_PROJECT_INDEX_DIR = REPO_ROOT / "data" / "memory"


def _load_knowledge_index_for(project: str) -> str:
    """Per-project knowledge index, falls back to global.
    Project file convention: data/memory/knowledge-<project>.md (lowercase).
    Cached for the daemon process lifetime — invalidated only by restart.
    Cap at 6KB so we don't blow prompt budget."""
    cache = _load_knowledge_index_for._cache  # type: ignore[attr-defined]
    key = (project or "").lower()
    if key in cache:
        return cache[key]
    candidates = []
    if key:
        candidates.append(_PROJECT_INDEX_DIR / f"knowledge-{key}.md")
    candidates.extend(_GLOBAL_INDEX_PATHS)
    text = "(knowledge_index.md not available)"
    for p in candidates:
        try:
            if p.exists():
                text = p.read_text(errors="replace")[:6000]
                break
        except Exception:
            continue
    cache[key] = text
    return text


_load_knowledge_index_for._cache = {}  # type: ignore[attr-defined]

# Test-first dev mode — projects listed here get a TDD instruction prepended
# to the dev prompt so the LLM writes the test before the implementation.
TEST_FIRST_PROJECTS = {
    p.strip() for p in os.environ.get("TEST_FIRST_PROJECTS", "").split(",")
    if p.strip()
}

DEV_WORKER_ID = os.environ.get("DEV_WORKER_ID", "1")
CURSOR_FILE = REPO_ROOT / "state" / f"axentx-dev-cursor-{DEV_WORKER_ID}.json"
NEW_TASK_INTERVAL = int(os.environ.get("DEV_DAEMON_INTERVAL_SEC", "300"))

DEV_SYSTEM = """You are a CODE-ONLY engineer for the axentx product family.

★ CRITICAL ROLE BOUNDARY ★
- You ONLY write executable source files. You DO NOT write plans,
  diagnoses, design documents, ADRs, PRDs, READMEs, or strategy notes.
- Plans/specs are produced by other agents (prd-daemon, architect-daemon,
  design-thinking-daemon). They give you concrete tasks. You ship code.
- If the task you receive is too vague to ship a real file (no concrete
  file paths, no acceptance criteria), DO NOT INVENT A PLAN. Instead emit
  the single special block below and stop:

```clarify
{
  "need_clarification": true,
  "reason": "<specific: what's missing — files? acceptance? tech-stack?>",
  "request_to": "prd-daemon|architect-daemon|design-thinking",
  "minimal_spec_needed": "<1-line description of the smallest spec that\
    would let you write code>"
}
```

OUTPUT FORMAT for actual code (when spec IS concrete enough):
For each new/changed file, emit a fenced code block with the FULL file
contents and a path comment as the FIRST line of the block:

```python
# path/to/file.py
<entire file content>
```

```typescript
// src/components/Foo.tsx
<entire file content>
```

```sql
-- migrations/0042_add_index.sql
<entire file content>
```

Hard rules:
- ALWAYS the WHOLE FILE — never partial diffs (commit-daemon overwrites).
- Path comment MUST be the very first line inside the fence.
- Include tests if the project has a test dir (mirror the layout).
- 1 file per fenced block. Multiple files = multiple blocks.
- NO markdown plans, NO "## Diagnosis" / "## Proposed change" / "## Plan"
  sections. ONE final "## Summary" (≤4 bullets) is the ONLY allowed prose.
- NEVER write a *.md file unless the spec EXPLICITLY says "create / update
  /docs/X.md". README/PLAN/DESIGN files are for OTHER agents to write.
- Never ask clarifying questions in chat — use the clarify block above.
"""

PROMPT_TPL = """Project: {project} (located at {repo_path})
Focus: {focus}

Past patterns + lessons learned (apply these — don't re-discover):
{knowledge_index}


Recent commits in this repo:
{git_log}

Project README excerpt:
{readme}

Last 3 swarm-shared decisions for this project:
{prior_decisions}

Existing PRD/spec for this product (extracted by prd-daemon, may be empty
if early-stage):
{spec_excerpt}

Active features/epics this project should ship next (from feature-synth or
prd output):
{feature_targets}

Task: ship CODE for the most valuable next increment under the {focus} \
focus. If spec_excerpt is empty, write the FOUNDATION files (entry point, \
core module, tests, package config) so future cycles have something to \
build on. NEVER write a "plan" — write actual files.

Output: code blocks (one per file, path comment as first line inside the \
fence — see system prompt) followed by a short ## Summary section.
"""


def load_cursor() -> dict:
    if CURSOR_FILE.exists():
        try: return json.loads(CURSOR_FILE.read_text())
        except: pass
    return {"rotation_idx": (int(DEV_WORKER_ID)-1) % len(ROTATION), "focus_idx": 0}


def save_cursor(c: dict) -> None:
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(json.dumps(c, indent=2))


def repo_context(project: str) -> tuple[str, str, str]:
    """git log + README excerpt + prior decisions for this project."""
    repo = PROJECTS_ROOT / project
    git_log = "(no git history)"
    readme = "(no README)"
    if (repo / ".git").exists():
        try:
            git_log = subprocess.run(
                ["git", "-C", str(repo), "log", "--oneline", "-10"],
                capture_output=True, text=True, timeout=10).stdout.strip() or "(empty)"
        except Exception: pass
    for fname in ("README.md", "readme.md", "README"):
        if (repo / fname).exists():
            readme = (repo / fname).read_text(errors="replace")[:2000]
            break

    # Prior decisions for this project from swarm-shared
    decisions_dir = REPO_ROOT / "state" / "swarm-shared" / "decisions"
    prior = "(no prior decisions)"
    if decisions_dir.exists():
        files = sorted(
            (f for f in decisions_dir.glob("*") if project.lower() in f.name.lower()),
            key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        if files:
            prior = "\n".join(f"- {f.name}: {f.read_text()[:300]}" for f in files)
    return git_log, readme, prior


MAX_REVIEW_BACKLOG = int(os.environ.get("DEV_MAX_REVIEW_BACKLOG", "15"))


def refine_rejected_task(src_path, item) -> bool:
    """Pick up a reviewer-rejected dev task, feed the reject reason back to
    the LLM so the next attempt addresses the specific blockers, and re-push
    to review-queue. This is the self-improvement loop — every rejection
    becomes training signal for the immediate next attempt."""
    project = item.get("project", "?")
    focus = item.get("focus", "?")
    attempts = int(item.get("dev_attempts", 1))  # already 1 from initial dev pass
    rejected_text = item.get("current", {}).get("text", "")
    repo_path = PROJECTS_ROOT / project

    log("dev", f"↺ refine {item['id']} ({project}/{focus}) attempt {attempts+1}")

    git_log, readme, prior = repo_context(project)
    test_first_prefix = (
        "Write the test FIRST, then the implementation. Show the failing test, "
        "then the code that makes it pass.\n\n"
        if project in TEST_FIRST_PROJECTS else ""
    )
    refine_prompt = (
        f"{test_first_prefix}"
        f"REFINEMENT — your previous attempt was rejected. The reviewer's "
        f"specific feedback is below. Address each cited blocker concretely "
        f"by emitting REAL CODE FILES (not plans).\n\n"
        f"=== reviewer feedback ===\n{rejected_text[:3500]}\n\n"
        f"=== project context ===\nProject: {project}  ({repo_path})\n"
        f"Focus: {focus}\nGit log:\n{git_log}\n\nREADME:\n{readme[:1500]}\n\n"
        f"=== task ===\nFix every blocker by SHIPPING CODE. Emit one fenced "
        f"code block per file, with the path comment as the FIRST line "
        f"inside the fence (e.g. '# src/foo.py' or '// src/Foo.tsx'). Always "
        f"emit the WHOLE file. After all code blocks, a short ## Summary."
    )
    try:
        out = synthesize(refine_prompt, system=DEV_SYSTEM, n_attempts=2,
                         max_tokens=DEV_REFINE_BUDGET, timeout=50)
    except Exception as e:
        log("dev", f"✗ refine LLM failed: {e}")
        return False

    item["dev_attempts"] = attempts + 1
    item["history"].append({
        "stage": "dev",
        "actor": "claude/llm-fallback-chain",
        "output": out[:6000],
        "at": datetime.datetime.utcnow().isoformat() + "Z",
        "is_refinement": True,
        "addresses_attempt": attempts,
    })
    if "current" not in item or not isinstance(item.get("current"), dict):
        item["current"] = {"text": ""}
    item["current"]["text"] = out[:6000]
    advance(item, src_path, "review", "dev", out)
    log("dev", f"✓ {item['id']} refined → review-queue (attempt {attempts+1})")

    # Self-knowledge hook 2026-05-04: every 25 successful refines, log a
    # lesson to shared_memory so other agents (across hosts) learn from
    # the pattern. knowledge-ingest pushes to HF dataset for training.
    try:
        global _N_SUCCESS
        _N_SUCCESS = globals().get("_N_SUCCESS", 0) + 1
        globals()["_N_SUCCESS"] = _N_SUCCESS
        if _N_SUCCESS % 25 == 0:
            from axentx_shared import memory_log
            memory_log("dev", "event",
                       f"dev refined {_N_SUCCESS} items in this process",
                       body=(f"Latest: {item['id']} → {item.get('project','?')} "
                             f"on attempt {attempts+1}. "
                             f"Pattern: refine-with-reviewer-feedback works "
                             f"when LLM chain has >2 ready providers."),
                       tags=["dev", "milestone", str(_N_SUCCESS)])
    except Exception:
        pass
    return True


def do_one_cycle() -> bool:
    # Step 1: drain rejected items first — they have reviewer feedback that
    # makes them MORE likely to converge than a fresh task. Self-improvement
    # loop: reject → refine with feedback → re-review.
    rejected = pick_oldest("dev")
    if rejected:
        return refine_rejected_task(*rejected)

    # Step 2: backpressure — don't generate new tasks if downstream is jammed.
    review_q = REPO_ROOT / "state" / "swarm-shared" / "review-queue"
    n_pending = len(list(review_q.glob("*.json"))) if review_q.exists() else 0
    if n_pending >= MAX_REVIEW_BACKLOG:
        log("dev", f"backpressure: review-queue {n_pending} ≥ {MAX_REVIEW_BACKLOG}, idle")
        return False

    # Step 3: create fresh task from rotation.
    cursor = load_cursor()
    project = ROTATION[cursor["rotation_idx"] % len(ROTATION)]
    focus = FOCUS_CYCLE[cursor["focus_idx"] % len(FOCUS_CYCLE)]
    repo_path = PROJECTS_ROOT / project
    if not repo_path.exists():
        log("dev", f"⚠ {project} not cloned at {repo_path} — skipping")
        cursor["rotation_idx"] = (cursor["rotation_idx"] + 1) % len(ROTATION)
        save_cursor(cursor)
        return False
    git_log, readme, prior = repo_context(project)
    knowledge_index = _load_knowledge_index_for(project)
    # ── Tech-stack constraint (added 2026-05-04 after user feedback:
    #    'มึงเขียน java ผสม python — tech lead ต้องวางก่อน'). dev MUST
    #    read /opt/axentx/<slug>/decisions/tech-stack.md and obey it.
    stack_md_path = repo_path / "decisions" / "tech-stack.md"
    stack_constraint = ""
    if stack_md_path.exists():
        try:
            stack_constraint = (
                "\n\n# LOCKED TECH STACK (do NOT diverge)\n"
                + stack_md_path.read_text(errors="replace")[:1500])
        except Exception:
            pass
    if not stack_constraint:
        stack_constraint = (
            "\n\n# WARNING: no decisions/tech-stack.md exists yet.\n"
            "If your ticket would introduce a primary language, emit "
            "a ```clarify``` block requesting tech-lead to lock the stack "
            "first — do NOT just pick one yourself.")
    # Pull spec / feature targets from disk so dev writes code FROM SPEC,
    # not just from focus heuristic. Specs come from prd-daemon (in /specs/)
    # and feature-targets from feature-synth (in /business/feature-queue.md).
    spec_excerpt = ""
    try:
        specs_dir = repo_path / "specs"
        if specs_dir.exists():
            md_files = sorted(specs_dir.glob("*.md"))[-3:]   # last 3 PRDs
            for p in md_files:
                spec_excerpt += f"\n\n## {p.name}\n" + p.read_text(
                    errors="replace")[:1500]
    except Exception:
        pass
    if not spec_excerpt:
        spec_excerpt = "(no PRD/spec yet — write FOUNDATION files)"

    feature_targets = ""
    try:
        fq = repo_path / "business" / "feature-queue.md"
        if fq.exists():
            feature_targets = fq.read_text(errors="replace")[:1500]
    except Exception:
        pass
    if not feature_targets:
        feature_targets = "(no specific features targeted — pick highest-value next)"

    prompt = PROMPT_TPL.format(
        project=project, repo_path=repo_path,
        focus=focus, git_log=git_log, readme=readme, prior_decisions=prior,
        knowledge_index=knowledge_index,
        spec_excerpt=spec_excerpt[:3000],
        feature_targets=feature_targets[:1200])
    prompt = prompt + stack_constraint
    if project in TEST_FIRST_PROJECTS:
        prompt = ("Write the test FIRST, then the implementation. Show the "
                  "failing test, then the code that makes it pass.\n\n" + prompt)
    # Synthesis pass = 3 LLM attempts + 1 synth. Heavier but better quality.
    # Toggle SYNTH_DEV=0 to fall back to single call_llm.
    synth_enabled = os.environ.get("SYNTH_DEV", "1") == "1"
    log("dev", f"▸ {project} / {focus}{' [synth=3]' if synth_enabled else ''}")
    try:
        if synth_enabled:
            out = synthesize(prompt, system=DEV_SYSTEM, n_attempts=3,
                             max_tokens=DEV_BUDGET, timeout=45)
        else:
            out = call_llm(prompt, system=DEV_SYSTEM, max_tokens=DEV_BUDGET, timeout=45)
    except Exception as e:
        log("dev", f"✗ LLM failed: {e}")
        # CRITICAL: advance cursor EVEN ON FAILURE so we don't get stuck
        # hammering the same project repeatedly. Observed 2026-05-02:
        # workio cycle hit LLM 429 storm → cursor frozen on workio →
        # every subsequent cycle re-tries workio → 60+ min commit drought.
        # Fix: rotate project on failure so other projects get a chance
        # while LLM cools off. Failed project will get its turn next round.
        cursor["rotation_idx"] = (cursor["rotation_idx"] + 1) % len(ROTATION)
        if cursor["rotation_idx"] == 0:
            cursor["focus_idx"] = (cursor["focus_idx"] + 1) % len(FOCUS_CYCLE)
        save_cursor(cursor)
        return False

    # Persist as decision record for future context
    decisions_dir = REPO_ROOT / "state" / "swarm-shared" / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    decision_path = decisions_dir / f"{ts}_{project}_{focus}.md"
    decision_path.write_text(f"# {project} / {focus}\n\n{out}\n")

    # Push into review queue
    item = new_item(project, focus, prompt)
    item["history"].append({
        "stage": "dev",
        "actor": "claude/llm-fallback-chain",
        "output": out[:6000],
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    if "current" not in item or not isinstance(item.get("current"), dict):
        item["current"] = {"text": ""}
    item["current"]["text"] = out[:6000]
    item["stage"] = "review"
    write_item(item, "review")
    log("dev", f"✓ {item['id']} → review-queue")

    # Advance cursor (rotate project, focus shifts every full project rotation)
    cursor["rotation_idx"] = (cursor["rotation_idx"] + 1) % len(ROTATION)
    if cursor["rotation_idx"] == 0:
        cursor["focus_idx"] = (cursor["focus_idx"] + 1) % len(FOCUS_CYCLE)
    save_cursor(cursor)
    return True


if __name__ == "__main__":
    # Per-worker role label so each @1..@6 instance has a unique heartbeat
    # entry on /dash/agents (without it, all 6 collapse onto agent:dev).
    role = f"dev-{os.environ.get('DEV_WORKER_ID', '1')}"
    daemon_loop(role, NEW_TASK_INTERVAL, do_one_cycle)
