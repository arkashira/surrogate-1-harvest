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
import time
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
        "qwen-coder", "git-clone", "git-pull", "redis", "parquet-direct-ingest", "bulk-ingest-parallel", "rag-vector-builder", "auto-orchestrate-continuous", "dataset-enrich", "hf-dataset-discoverer", "dedup-bootstrap", "github-agentic-crawler", "ollama-pull-granite", "synthetic-data", "self-ingest", "scrape-sre-postmortems", "refresh-cve-feed", "self-heal-watchdog", "gh-actions-ticker", "llm-burst-generator", "expand-role-keywords", "kaggle-trainer", "lightning-trainer", "dataset-mirror",
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


@app.get("/dynamic-datasets")
def dynamic_datasets():
    """Expose the discoverer's running list of auto-found datasets so
    external runners (GitHub Actions, Oracle Free Tier, etc.) can sync it
    and ingest the same expanding catalog without each having to re-run
    the discoverer themselves."""
    p = HOME / ".surrogate/state/dynamic-datasets.json"
    if not p.exists():
        return JSONResponse({"datasets": [], "note": "dynamic-datasets.json not yet built"}, status_code=200)
    try:
        return PlainTextResponse(p.read_text(), media_type="application/json")
    except Exception as e:
        raise HTTPException(500, f"read failed: {e}")


# ── Cursor / stamp-and-move state (the "don't re-pull row 0 every time" fix) ──
# Stored as ~/.surrogate/state/cursors.db (SQLite). Each row = (slug, offset, ts).
# Runners GET /cursor/{slug} before streaming, then POST /cursor/{slug}/advance
# with how many rows they processed. Next runner picks up where the last left off.
import sqlite3 as _sql_for_cursor

_CURSOR_DB = HOME / ".surrogate/state/cursors.db"

def _cursor_conn():
    _CURSOR_DB.parent.mkdir(parents=True, exist_ok=True)
    c = _sql_for_cursor.connect(str(_CURSOR_DB), check_same_thread=False, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS cursors (
            slug   TEXT PRIMARY KEY,
            offset INTEGER NOT NULL DEFAULT 0,
            ts     INTEGER NOT NULL
        )
    """)
    return c


@app.get("/cursor/{slug:path}")
def get_cursor(slug: str):
    """Return the next-row-to-process offset for this dataset slug.
    Default 0 if never seen. Runners SHOULD itertools.islice(stream, offset, offset+cap)."""
    try:
        c = _cursor_conn()
        row = c.execute("SELECT offset, ts FROM cursors WHERE slug = ?", (slug,)).fetchone()
        return {"slug": slug, "offset": row[0] if row else 0, "ts": row[1] if row else 0}
    except Exception as e:
        raise HTTPException(500, f"cursor read: {e}")


class CursorAdvance(BaseModel):
    n: int


@app.post("/cursor/{slug:path}/advance")
def advance_cursor(slug: str, body: CursorAdvance):
    """Advance the cursor by N rows. Atomic via SQLite UPSERT.
    Idempotent — same call with same n yields same final offset only if
    sequential; concurrent calls race-add (fine, dedup catches the rest)."""
    try:
        c = _cursor_conn()
        ts = int(time.time())
        c.execute("""
            INSERT INTO cursors (slug, offset, ts) VALUES (?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                offset = offset + excluded.offset,
                ts = excluded.ts
        """, (slug, body.n, ts))
        c.commit()
        new_offset = c.execute("SELECT offset FROM cursors WHERE slug = ?", (slug,)).fetchone()[0]
        return {"slug": slug, "advanced_by": body.n, "new_offset": new_offset, "ts": ts}
    except Exception as e:
        raise HTTPException(500, f"cursor advance: {e}")


@app.get("/cursor")
def list_cursors(limit: int = 100):
    """List all cursors — useful for ops dashboard."""
    try:
        c = _cursor_conn()
        rows = c.execute(
            "SELECT slug, offset, ts FROM cursors ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"cursors": [{"slug": s, "offset": o, "ts": t} for s, o, t in rows]}
    except Exception as e:
        raise HTTPException(500, f"cursor list: {e}")


class ChatRequest(BaseModel):
    prompt: str
    cwd: str | None = None
    max_steps: int = 12
    timeout_sec: int = 180




@app.get("/selftest")
def selftest() -> dict:
    """Verify HF Space environment — catches Mac-mindset bugs early.
    Tests: critical imports, hardcoded path leaks, key file existence."""
    results = {"ok": True, "checks": {}}
    
    # 1. Required imports
    for mod in ["datasets", "huggingface_hub", "pyarrow", "numpy", "sqlite3"]:
        try:
            __import__(mod)
            results["checks"][f"import_{mod}"] = True
        except ImportError as e:
            results["checks"][f"import_{mod}"] = False
            results["ok"] = False
    
    # 2. Critical paths exist (HF Space side)
    for path_str in ["~/.surrogate/bin", "~/.surrogate/state", "~/.surrogate/logs"]:
        p = Path(os.path.expanduser(path_str))
        results["checks"][f"path_{path_str}"] = p.exists()
        if not p.exists():
            results["ok"] = False
    
    # 3. No Mac path leaks in active scripts
    bad_paths = []
    for f in (HOME / ".surrogate/bin").rglob("*.sh"):
        try:
            text_content = f.read_text(errors="ignore")
            if "/Users/Ashira" in text_content:
                bad_paths.append(f.name)
        except Exception:
            pass
    results["checks"]["no_mac_paths"] = len(bad_paths) == 0
    if bad_paths:
        results["ok"] = False
        results["mac_path_leaks"] = bad_paths[:10]
    
    # 4. HF token present
    results["checks"]["hf_token_set"] = bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
    
    return results


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
