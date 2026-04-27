#!/usr/bin/env python3
"""
Hermes status HTTP server for HF Space.
FastAPI + uvicorn — robust port binding, auto-handles signals.

Endpoints:
  GET /         → JSON status (ledger size, episodes, daemons, disk)
  GET /health   → simple {"ok": true}
  GET /logs     → tail of recent boot/cron logs (debug)
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

app = FastAPI(title="hermes", docs_url=None, redoc_url=None)

HOME = Path(os.environ.get("HOME", "/home/hermes"))
LEDGER = HOME / ".claude/state/scrape-ledger.db"
EPISODES = HOME / ".claude/state/surrogate-memory/episodes.jsonl"
LOG_DIR = HOME / ".claude/logs"


def _ledger_count() -> int:
    try:
        with sqlite3.connect(str(LEDGER), timeout=2) as c:
            return c.execute("SELECT COUNT(*) FROM scraped").fetchone()[0]
    except Exception:
        return 0


def _episodes_count() -> int:
    try:
        if EPISODES.exists():
            return sum(1 for _ in EPISODES.open())
    except Exception:
        pass
    return 0


def _daemons() -> int:
    try:
        out = subprocess.run(
            ["pgrep", "-fc", "discord-bot|surrogate-dev|scrape-loop|hermes-cron|ollama"],
            capture_output=True, text=True, timeout=2,
        )
        return int(out.stdout.strip() or 0)
    except Exception:
        return 0


@app.get("/")
def root() -> JSONResponse:
    return JSONResponse({
        "service": "hermes",
        "model": "axentx/surrogate-1",
        "status": "ok",
        "ts": datetime.now(timezone.utc).isoformat(),
        "ledger_repos": _ledger_count(),
        "episodes": _episodes_count(),
        "daemons_running": _daemons(),
    })


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/logs")
def logs() -> PlainTextResponse:
    out_lines: list[str] = []
    for log_name in ("boot.log", "cron.log", "discord-bot.log", "ollama.log"):
        f = LOG_DIR / log_name
        if not f.exists():
            continue
        try:
            tail = f.read_text(errors="replace").splitlines()[-10:]
            out_lines.append(f"━━━ {log_name} ━━━")
            out_lines.extend(tail)
            out_lines.append("")
        except Exception:
            pass
    return PlainTextResponse("\n".join(out_lines) or "(no logs)")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860, log_level="info")
