"""Checkpoint store — JSONL event log per task, append-only.

Purpose:
  - Crash-safe: every event appended immediately (no buffering)
  - Resume-aware: load full event trail to reconstruct task state
  - Distill-friendly: each file = complete conversation trace a future model can learn from

Event types:
  task_start, codebase_review, provider_selected, stream_chunk, model_switch,
  result_draft, review_requested, review_verdict, revision_requested, task_done,
  task_failed, provider_probe

File layout:
  ~/.surrogate/yolo/checkpoints/<task-id>.jsonl    — live tasks
  ~/.surrogate/yolo/checkpoints_done/<task-id>.jsonl  — completed (archive)
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

CHECKPOINT_DIR = Path.home() / ".surrogate" / "yolo" / "checkpoints"
CHECKPOINT_DONE = Path.home() / ".surrogate" / "yolo" / "checkpoints_done"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass
class Checkpoint:
    task_id: str
    path: Path

    @classmethod
    def open(cls, task_id: str) -> "Checkpoint":
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        return cls(task_id=task_id, path=CHECKPOINT_DIR / f"{task_id}.jsonl")

    def append(self, event_type: str, **fields: Any) -> None:
        """Atomically append event. Fields serialize via JSON."""
        rec = {"t": _now(), "event": event_type, **fields}
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def last_event(self, event_type: str = "") -> dict | None:
        for e in reversed(self.events()):
            if not event_type or e.get("event") == event_type:
                return e
        return None

    def resume_state(self) -> dict:
        """Reconstruct what we know from the event trail.

        Returns:
          {
            "started": bool,
            "completed": bool,
            "failed": bool,
            "current_model": str | None,
            "draft_text": str (partial output so far),
            "attempts": int,
            "last_event": dict | None,
            "artifacts_reviewed": list[str],
            "review_iterations": int,
          }
        """
        ev = self.events()
        state = {
            "started": False,
            "completed": False,
            "failed": False,
            "current_model": None,
            "draft_text": "",
            "attempts": 0,
            "last_event": ev[-1] if ev else None,
            "artifacts_reviewed": [],
            "review_iterations": 0,
        }
        for e in ev:
            etype = e.get("event")
            if etype == "task_start":
                state["started"] = True
            elif etype == "provider_selected":
                state["current_model"] = e.get("model")
                state["attempts"] += 1
            elif etype == "model_switch":
                state["current_model"] = e.get("to")
            elif etype == "codebase_review":
                state["artifacts_reviewed"] = e.get("artifacts", [])
            elif etype == "result_draft":
                state["draft_text"] = e.get("text", state["draft_text"])
            elif etype == "review_verdict":
                state["review_iterations"] += 1
            elif etype == "task_done":
                state["completed"] = True
            elif etype == "task_failed":
                state["failed"] = True
        return state

    def archive(self) -> None:
        """Move to checkpoints_done/ after task complete."""
        CHECKPOINT_DONE.mkdir(parents=True, exist_ok=True)
        dest = CHECKPOINT_DONE / self.path.name
        if self.path.exists():
            self.path.rename(dest)
            self.path = dest


def list_active() -> list[str]:
    if not CHECKPOINT_DIR.exists():
        return []
    return [p.stem for p in CHECKPOINT_DIR.glob("*.jsonl")]


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: checkpoint.py <task-id> [replay]")
        sys.exit(1)
    cp = Checkpoint.open(sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2] == "replay":
        for e in cp.events():
            print(json.dumps(e, ensure_ascii=False))
    else:
        state = cp.resume_state()
        print(json.dumps(state, indent=2, ensure_ascii=False, default=str))
