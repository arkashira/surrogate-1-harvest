#!/usr/bin/env python3
"""axentx PM daemon — sprint state machine. Tracks sprint number and
runs ceremony LLM calls when sprint boundaries are crossed.

Sprint boundaries:
- mini-sprint:  every 4 hours (planning + retro)
- big-sprint:   every Mon 09:00 BKK (= 02:00 UTC)
"""
from __future__ import annotations
import os, sys, json, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import REPO_ROOT, log, call_llm, daemon_loop

POLL_SEC = 60
STATE_FILE = REPO_ROOT / "state" / "axentx-pm-state.json"
SHARED = REPO_ROOT / "state" / "swarm-shared"

PM_SYSTEM = """You are a PM running ceremonies for the axentx product family. \
Be concise. Output as actionable bullet lists, no fluff."""


def load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except: pass
    return {"last_mini": "1970-01-01T00:00:00Z",
            "last_big": "1970-01-01T00:00:00Z",
            "sprint_n": 0}


def save_state(s: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2))


def hours_since(iso: str) -> float:
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return 1e6
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600


def recent_decisions(n: int = 20) -> str:
    """Pull last N decisions across all projects for context."""
    decisions = sorted(
        (SHARED / "decisions").glob("*.md"),
        key=lambda p: p.stat().st_mtime, reverse=True)[:n]
    if not decisions: return "(no decisions yet)"
    return "\n".join(f"- {d.name}: {d.read_text()[:200]}" for d in decisions)


def run_mini_sprint() -> str:
    decs = recent_decisions(15)
    prompt = (
        "Run a mini-sprint planning ceremony.\n\n"
        f"Recent decisions across all axentx projects:\n{decs}\n\n"
        "Output:\n"
        "1. **Wins** (3-5) since last sprint\n"
        "2. **Blockers** (2-4)\n"
        "3. **Next sprint goals** (3-5, per project, scoped to <4h)\n"
        "4. **Risks** (2-3)")
    return call_llm(prompt, system=PM_SYSTEM, max_tokens=1200, timeout=40)


def run_mini_retro() -> str:
    decs = recent_decisions(20)
    prompt = (
        "Run a mini-retrospective.\n\n"
        f"Recent decisions:\n{decs}\n\n"
        "Output:\n"
        "1. **What went well** (3-5)\n"
        "2. **What didn't** (3-5)\n"
        "3. **Process improvements** for the autonomous pipeline (3-5 actionable)\n"
        "4. **Quality score** 0-10 with justification")
    return call_llm(prompt, system=PM_SYSTEM, max_tokens=1200, timeout=40)


def do_one_pm_check() -> bool:
    state = load_state()
    did_anything = False

    # mini-sprint every 4h
    if hours_since(state["last_mini"]) >= 4:
        try:
            log("pm", "▸ running mini-sprint")
            out_plan = run_mini_sprint()
            log("pm", "▸ running mini-retro")
            out_retro = run_mini_retro()
            ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            (SHARED / "past-cycles").mkdir(exist_ok=True)
            (SHARED / "past-cycles" / f"{ts}_mini-sprint.md").write_text(
                f"# mini-sprint #{state['sprint_n']+1}\n\n## planning\n{out_plan}\n\n## retro\n{out_retro}")
            state["last_mini"] = datetime.datetime.utcnow().isoformat() + "Z"
            state["sprint_n"] = state.get("sprint_n", 0) + 1
            save_state(state)
            log("pm", f"✓ sprint #{state['sprint_n']} ceremonies recorded")
            did_anything = True
        except Exception as e:
            log("pm", f"✗ sprint ceremony failed: {e}")

    return did_anything


if __name__ == "__main__":
    daemon_loop("pm", POLL_SEC, do_one_pm_check)
