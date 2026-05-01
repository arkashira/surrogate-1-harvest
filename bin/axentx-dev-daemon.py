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
from axentx_pipeline import (REPO_ROOT, QUEUES, log, call_llm,
                             new_item, write_item, daemon_loop)

PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
ROTATION = ["Costinel", "Vanguard", "arkship", "Costinel", "Vanguard",
            "arkship", "surrogate", "workio"]
FOCUS_CYCLE = ["discovery", "design", "backend", "frontend", "quality", "ops"]
CURSOR_FILE = REPO_ROOT / "state" / "axentx-dev-cursor.json"
NEW_TASK_INTERVAL = int(os.environ.get("DEV_DAEMON_INTERVAL_SEC", "300"))

DEV_SYSTEM = """You are a senior full-stack engineer working autonomously on \
the axentx product family. For each task you receive: identify the highest-value \
incremental improvement that can ship in <2h, write a concrete implementation \
plan + code snippets if applicable. Output in markdown. Never ask clarifying \
questions — make best-judgement calls and proceed."""

PROMPT_TPL = """Project: {project} (located at {repo_path})
Focus: {focus}

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
    return {"rotation_idx": 0, "focus_idx": 0}


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


def do_one_cycle() -> bool:
    # Throttle: if reviewer hasn't caught up, don't pile on more dev output.
    # Pipeline naturally rate-limits to slowest consumer this way.
    review_q = REPO_ROOT / "state" / "swarm-shared" / "review-queue"
    n_pending = len(list(review_q.glob("*.json"))) if review_q.exists() else 0
    if n_pending >= MAX_REVIEW_BACKLOG:
        log("dev", f"backpressure: review-queue {n_pending} ≥ {MAX_REVIEW_BACKLOG}, idle")
        return False

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
    prompt = PROMPT_TPL.format(
        project=project, repo_path=repo_path,
        focus=focus, git_log=git_log, readme=readme, prior_decisions=prior)
    log("dev", f"▸ {project} / {focus}")
    try:
        out = call_llm(prompt, system=DEV_SYSTEM, max_tokens=2000, timeout=45)
    except Exception as e:
        log("dev", f"✗ LLM failed: {e}")
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
    daemon_loop("dev", NEW_TASK_INTERVAL, do_one_cycle)
