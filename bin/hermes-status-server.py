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

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

app = FastAPI(title="hermes", docs_url=None, redoc_url=None)

HOME = Path(os.environ.get("HOME", "/home/hermes"))
LEDGER = HOME / ".surrogate/state/scrape-ledger.db"
EPISODES = HOME / ".surrogate/state/surrogate-memory/episodes.jsonl"
LOG_DIR = HOME / ".surrogate/logs"


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
    """Count all surrogate daemons by name pattern."""
    try:
        out = subprocess.run(
            ["pgrep", "-fc",
             "discord-bot|surrogate-dev|scrape-loop|scrape-daemon|"
             "agentic-crawler|skill-synthesis|hermes-cron|ollama|"
             "domain-scrape|qwen-coder|auto-orchestrate"],
            capture_output=True, text=True, timeout=2,
        )
        return int(out.stdout.strip() or 0)
    except Exception:
        return 0


def _episodes_count_v2() -> int:
    """Count training pairs (current source of truth) instead of legacy episodes."""
    pairs = HOME / ".surrogate/training-pairs.jsonl"
    try:
        if pairs.exists():
            return sum(1 for _ in pairs.open())
    except Exception:
        pass
    # Fallback to old episodes path
    return _episodes_count()


def _training_pairs_count() -> int:
    pairs = HOME / ".surrogate/training-pairs.jsonl"
    try:
        if pairs.exists():
            return sum(1 for _ in pairs.open())
    except Exception:
        pass
    return 0


def _skill_count() -> int:
    skills = HOME / ".surrogate/skills"
    if not skills.exists():
        return 0
    return len(list(skills.glob("**/SKILL.md")))


def _agentic_visited() -> int:
    db = HOME / ".surrogate/state/agentic-frontier.db"
    try:
        with sqlite3.connect(str(db), timeout=2) as c:
            return c.execute("SELECT COUNT(*) FROM visited").fetchone()[0]
    except Exception:
        return 0


def _ollama_models() -> list[str]:
    """Quick (non-blocking) check of loaded Ollama models. Caches for 30s."""
    cache = HOME / ".surrogate/state/.ollama-models-cache.json"
    try:
        import json as _json, time
        if cache.exists():
            cached = _json.loads(cache.read_text())
            if time.time() - cached.get("ts", 0) < 30:
                return cached.get("models", [])
    except Exception:
        pass
    try:
        import urllib.request, json as _json, time
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5) as r:
            models = [m["name"] for m in _json.load(r).get("models", [])]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(_json.dumps({"ts": time.time(), "models": models}))
        return models
    except Exception:
        return []



def _dedup_count() -> int:
    """Total deduped hashes — single source of truth."""
    db = HOME / ".surrogate/state/dedup.db"
    try:
        with sqlite3.connect(str(db), timeout=2) as c:
            return c.execute("SELECT COUNT(*) FROM seen_hashes").fetchone()[0]
    except Exception:
        return 0


@app.get("/")
def root() -> JSONResponse:
    return JSONResponse({
        "service": "surrogate",
        "model": "axentx/surrogate-1",
        "status": "ok",
        "ts": datetime.now(timezone.utc).isoformat(),
        "ledger_repos": _ledger_count(),
        "training_pairs": _training_pairs_count(),
        "agentic_urls_visited": _agentic_visited(),
        "skills_synthesized": _skill_count(),
        "episodes": _episodes_count_v2(),
        "daemons_running": _daemons(),
        "models_loaded": _ollama_models(),
        "dedup_hashes": _dedup_count(),
    })


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/logs/{name}")
def log_tail(name: str, lines: int = 100) -> PlainTextResponse:
    """Tail a specific log file. Allowlist for security."""
    allowed = {
        "boot", "cron", "cron-master", "scrape-continuous", "scrape-daemon",
        "scrape-keyword-tuner", "agentic-crawler", "skill-synthesis",
        "auto-orchestrate-loop", "training-push", "ollama", "discord-bot",
        "hermes-discord-bot", "surrogate-research-loop", "surrogate-research-apply",
        "surrogate-dev-loop", "domain-scrape-loop", "github-domain-scrape",
        "qwen-coder", "git-clone", "git-pull", "redis", "rag-vector-builder", "auto-orchestrate-continuous", "dataset-enrich", "hf-dataset-discoverer", "dedup-bootstrap", "github-agentic-crawler", "ollama-pull-granite", "synthetic-data", "self-ingest", "scrape-sre-postmortems", "refresh-cve-feed",
        "ollama-pull-coder", "ollama-pull-devstral", "ollama-pull-fallback",
        "ollama-pull-yicoder", "ollama-pull-embed", "ollama-pull-light",
    }
    if name not in allowed:
        raise HTTPException(404, f"Unknown log: {name}. Allowed: {sorted(allowed)}")
    log_file = LOG_DIR / f"{name}.log"
    if not log_file.exists():
        return PlainTextResponse(f"# {name}.log does not exist yet", status_code=200)
    try:
        out = subprocess.run(
            ["tail", "-n", str(min(lines, 500)), str(log_file)],
            capture_output=True, text=True, timeout=5,
        )
        return PlainTextResponse(out.stdout)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/logs-list")
def logs_list() -> dict:
    """List all available log files."""
    if not LOG_DIR.exists():
        return {"logs": []}
    return {"logs": sorted(p.stem for p in LOG_DIR.glob("*.log"))}


class ChatRequest(BaseModel):
    prompt: str
    cwd: str | None = None
    max_steps: int = 12
    timeout_sec: int = 180


@app.post("/chat")
async def chat(req: ChatRequest) -> JSONResponse:
    """Run a prompt through the surrogate CLI inside the container, return result.
    Used by remote Surrogate CLI clients (Mac/laptop) to delegate to Hermes brain on HF.
    """
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")

    surrogate_bin = HOME / ".surrogate/bin/surrogate"
    if not surrogate_bin.exists():
        raise HTTPException(status_code=503, detail="surrogate CLI not installed in container")

    proc = await asyncio.create_subprocess_exec(
        str(surrogate_bin), "-p", req.prompt, "--max-steps", str(req.max_steps),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=req.cwd or str(HOME),
        env={**os.environ, "TERM": "dumb"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=req.timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(status_code=504, detail=f"timeout after {req.timeout_sec}s")

    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")

    # Strip ANSI for clean JSON output
    import re as _re
    out = _re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", out)
    out = _re.sub(r"\x1b\[\?[0-9]+[hl]", "", out)
    out = "\n".join(l for l in out.splitlines() if not l.strip().startswith(("⏺", "●"))).strip()

    return JSONResponse({
        "ok": proc.returncode == 0,
        "rc": proc.returncode or 0,
        "response": out or "(empty)",
        "stderr_tail": err[-1000:] if err else "",
    })


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
    import os, sys, uvicorn
    port = int(os.environ.get("PORT", "7860"))
    print(f"[hermes] starting uvicorn on 0.0.0.0:{port}", flush=True)
    print(f"[hermes] python={sys.version.split()[0]} home={HOME}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", access_log=True)
