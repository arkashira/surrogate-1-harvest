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
                             pick_oldest, advance, get_role_budget)

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

DEV_SYSTEM = """You are a senior full-stack engineer working autonomously on \
the axentx product family. For each task you receive: identify the highest-value \
incremental improvement that can ship in <2h, write a concrete implementation \
plan + code snippets if applicable. Output in markdown. Never ask clarifying \
questions — make best-judgement calls and proceed."""

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

Task: pick the most valuable next improvement for this project under the \
{focus} focus. Output sections:
1. **Diagnosis** — what is missing / broken / weak (3-5 bullets)
2. **Proposed change** — concrete file/line scope
3. **Implementation** — code/diff or step-by-step
4. **Verification** — how to confirm it works
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
        f"specific feedback is below. Address each cited blocker concretely.\n\n"
        f"=== reviewer feedback ===\n{rejected_text[:3500]}\n\n"
        f"=== project context ===\nProject: {project}  ({repo_path})\n"
        f"Focus: {focus}\nGit log:\n{git_log}\n\nREADME:\n{readme[:1500]}\n\n"
        f"=== task ===\nProduce a CONCRETE implementation that resolves every "
        f"blocker the reviewer cited. Output sections: Diagnosis, Proposed "
        f"change (specific files/lines), Implementation (actual code/diff, "
        f"not 'add documentation' — show the lines), Verification."
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
    item["current"]["text"] = out[:6000]
    advance(item, src_path, "review", "dev", out)
    log("dev", f"✓ {item['id']} refined → review-queue (attempt {attempts+1})")
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
    prompt = PROMPT_TPL.format(
        project=project, repo_path=repo_path,
        focus=focus, git_log=git_log, readme=readme, prior_decisions=prior,
        knowledge_index=knowledge_index)
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
