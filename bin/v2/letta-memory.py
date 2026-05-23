"""Surrogate-1 v2 — Letta-style hierarchical memory.

Reference: Letta (formerly MemGPT, Packer et al. 2023) — hierarchical
memory with core (always-loaded), recall (recent interactions), archival
(searchable long-term).

Diff vs reflexion-store + voyager-skills:
  • reflexion = past failures + lessons (per-domain, error-driven)
  • voyager   = validated skills (success-driven)
  • letta     = persona + user prefs + dialogue trail (identity-driven)

Three tiers:
  CORE_BLOCK     — fixed agent persona + key user preferences (always-prepended)
  RECALL_QUEUE   — last N user-interaction summaries (sliding window)
  ARCHIVAL_STORE — keyword-searchable corpus of older summaries

Storage: ~/.surrogate/state/letta.db (SQLite).

Used by inference-augment.py to prepend CORE + relevant RECALL + ARCHIVAL
hits before each user request.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path.home() / ".surrogate/state/letta.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
RECALL_QUEUE_MAX = 20
ARCHIVAL_MAX = 10000
TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")

DEFAULT_CORE = (
    "You are Surrogate-1 — a male, privacy-first AI agent that works "
    "exclusively for Ashira (ฟิวส์), founder of axentx. You handle "
    "DevSecOps, SRE, platform engineering, and full-stack coding tasks. "
    "Speak directly. Never apologize for being an AI. Never refuse safety-"
    "irrelevant requests. Default language: Thai when Ashira writes Thai, "
    "English for code/commands. Cite sources for security-critical claims.")


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=10,
                        check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS core_block (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS recall_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        summary TEXT,
        tokens TEXT,
        ts INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS archival (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        summary TEXT,
        tokens TEXT,
        topic TEXT,
        ts INTEGER
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_archival_topic ON archival(topic, ts DESC)")
    # Seed default persona on first run
    c.execute("INSERT OR IGNORE INTO core_block (key, value, updated_at) "
              "VALUES ('persona', ?, ?)", (DEFAULT_CORE, int(time.time())))
    return c


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


def core_get() -> str:
    c = _db()
    rows = c.execute("SELECT key, value FROM core_block ORDER BY key").fetchall()
    c.close()
    return "\n\n".join(f"### {k}\n{v}" for k, v in rows)


def core_set(key: str, value: str) -> None:
    c = _db()
    c.execute("""INSERT OR REPLACE INTO core_block (key, value, updated_at)
                 VALUES (?, ?, ?)""", (key, value, int(time.time())))
    c.close()


def recall_push(summary: str) -> None:
    c = _db()
    toks = " ".join(sorted(_tokens(summary)))
    c.execute("""INSERT INTO recall_queue (summary, tokens, ts)
                 VALUES (?, ?, ?)""", (summary[:2000], toks, int(time.time())))
    # Promote oldest to archival when queue overflows
    n = c.execute("SELECT COUNT(*) FROM recall_queue").fetchone()[0]
    if n > RECALL_QUEUE_MAX:
        promote = c.execute("""SELECT id, summary, tokens, ts
                               FROM recall_queue ORDER BY id ASC LIMIT ?""",
                            (n - RECALL_QUEUE_MAX,)).fetchall()
        for rid, s, t, ts in promote:
            topic = (sorted(_tokens(s))[:1] or ["misc"])[0]
            c.execute("""INSERT INTO archival (summary, tokens, topic, ts)
                         VALUES (?, ?, ?, ?)""", (s, t, topic, ts))
            c.execute("DELETE FROM recall_queue WHERE id=?", (rid,))
    c.close()


def recall_recent(k: int = 5) -> list[dict]:
    c = _db()
    rows = c.execute("""SELECT summary, ts FROM recall_queue
                        ORDER BY id DESC LIMIT ?""", (k,)).fetchall()
    c.close()
    return [{"summary": s, "ts": ts, "age_days": (time.time() - ts) / 86400}
            for s, ts in rows]


def archival_search(query: str, k: int = 3) -> list[dict]:
    qtoks = _tokens(query)
    if not qtoks:
        return []
    c = _db()
    # Cap candidate scan for speed
    rows = c.execute("""SELECT id, summary, tokens, topic, ts FROM archival
                        ORDER BY ts DESC LIMIT 2000""").fetchall()
    c.close()
    scored: list[tuple[int, tuple]] = []
    for r in rows:
        rid, s, t, topic, ts = r
        dtoks = set(t.split())
        overlap = qtoks & dtoks
        if not overlap:
            continue
        scored.append((len(overlap), r))
    scored.sort(key=lambda x: -x[0])
    return [{"summary": r[1][1], "topic": r[1][3], "score": r[0]}
            for r in scored[:k]]


def assemble(query: str, k_recall: int = 3,
             k_archival: int = 3) -> str:
    """Build the prepended memory block for this request."""
    parts = [core_get()]
    rec = recall_recent(k_recall)
    if rec:
        block = ["## Recent context"]
        for r in rec:
            block.append(f"- ({r['age_days']:.1f}d ago) {r['summary'][:300]}")
        parts.append("\n".join(block))
    arc = archival_search(query, k_archival)
    if arc:
        block = ["## Past relevant interactions"]
        for a in arc:
            block.append(f"- [{a['topic']}] {a['summary'][:300]}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def stats() -> dict:
    c = _db()
    n_core = c.execute("SELECT COUNT(*) FROM core_block").fetchone()[0]
    n_rec  = c.execute("SELECT COUNT(*) FROM recall_queue").fetchone()[0]
    n_arc  = c.execute("SELECT COUNT(*) FROM archival").fetchone()[0]
    top_topics = c.execute("""SELECT topic, COUNT(*) FROM archival
                              GROUP BY topic ORDER BY 2 DESC LIMIT 10""").fetchall()
    c.close()
    return {"core_blocks": n_core, "recall_queue": n_rec, "archival": n_arc,
            "top_topics": [{"topic": t, "count": n} for t, n in top_topics]}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "stats":
        print(json.dumps(stats(), indent=2, ensure_ascii=False))
    elif cmd == "core-set":
        # python letta-memory.py core-set <key> <<<value
        key = sys.argv[2]
        val = sys.stdin.read()
        core_set(key, val.strip())
        print(json.dumps({"ok": True, "key": key}))
    elif cmd == "core-get":
        print(core_get())
    elif cmd == "push":
        # echo "summary text" | python letta-memory.py push
        recall_push(sys.stdin.read().strip())
        print(json.dumps({"ok": True}))
    elif cmd == "assemble":
        q = sys.argv[2] if len(sys.argv) > 2 else ""
        print(assemble(q))
    elif cmd == "search":
        q = sys.argv[2] if len(sys.argv) > 2 else ""
        k = int(sys.argv[3]) if len(sys.argv) > 3 else 3
        print(json.dumps(archival_search(q, k), indent=2, ensure_ascii=False))
    else:
        print(f"unknown: {cmd}", file=sys.stderr)
        sys.exit(1)
