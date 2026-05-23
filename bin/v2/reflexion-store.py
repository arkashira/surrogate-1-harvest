"""Surrogate-1 v2 — Reflexion bounded buffer.

Stores (task, failed_attempt, error, reflection, fix) tuples so the model
can retrieve "have I tried something like this before, and what did I learn?"
at inference time.

Inspired by Shinn et al. 2023 (Reflexion) but bounded + per-domain + with
keyword + bigram TF-IDF retrieval (no embedding model required — runs on
CPU-basic HF Space).

DB: ~/.surrogate/state/reflexion.db (SQLite WAL).
Pruned to max_per_domain rows on insert (drops lowest-score-oldest first).

Used by:
  - constitutional-loop.py (writes failures + reflections)
  - tool-trace-collector.py (writes tool-call failures)
  - serve-vllm.sh prompt template (reads top-k similar at inference)
"""
from __future__ import annotations
import hashlib
import json
import math
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Iterable

DB_PATH = Path.home() / ".surrogate/state/reflexion.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
MAX_PER_DOMAIN = 10000
TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=30,
                        check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS lessons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_hash TEXT,
        task_text TEXT,
        attempt TEXT,
        error TEXT,
        reflection TEXT,
        fix TEXT,
        domain TEXT,
        tokens TEXT,           -- space-joined unique tokens for keyword recall
        score REAL DEFAULT 0,  -- bumps when retrieved (recency × relevance)
        created_at INTEGER
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_lessons_domain ON lessons(domain, score DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_lessons_hash ON lessons(task_hash)")
    return c


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())[:200]


def store(task: str, attempt: str, error: str, reflection: str,
          fix: str, domain: str) -> int:
    """Add a lesson. Returns row id. Skips dup by task_hash + similar fix."""
    h = hashlib.md5(task.encode("utf-8")[:500]).hexdigest()[:16]
    toks = " ".join(sorted(set(_tokens(task + " " + error + " " + reflection))))
    c = _db()
    cur = c.execute("SELECT 1 FROM lessons WHERE task_hash=? AND domain=? LIMIT 1",
                    (h, domain))
    if cur.fetchone():
        c.close()
        return -1
    cur = c.execute("""INSERT INTO lessons
                       (task_hash, task_text, attempt, error, reflection,
                        fix, domain, tokens, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (h, task[:4000], attempt[:4000], error[:2000],
                     reflection[:2000], fix[:4000], domain, toks,
                     int(time.time())))
    rid = cur.lastrowid
    _prune(c, domain)
    c.close()
    return rid


def _prune(c: sqlite3.Connection, domain: str) -> None:
    cur = c.execute("SELECT COUNT(*) FROM lessons WHERE domain=?", (domain,))
    n = cur.fetchone()[0]
    if n <= MAX_PER_DOMAIN:
        return
    drop = n - MAX_PER_DOMAIN
    c.execute("""DELETE FROM lessons WHERE id IN (
        SELECT id FROM lessons WHERE domain=?
        ORDER BY score ASC, created_at ASC LIMIT ?)""", (domain, drop))


def retrieve_similar(task: str, domain: str | None = None,
                     k: int = 3) -> list[dict]:
    """Top-k lessons by token-overlap × IDF. Bumps retrieved rows' score."""
    qtoks = set(_tokens(task))
    if not qtoks:
        return []
    c = _db()
    where = "WHERE domain=?" if domain else ""
    args = (domain,) if domain else ()
    cur = c.execute(f"""SELECT id, task_text, error, reflection, fix, tokens,
                              created_at FROM lessons {where}
                       ORDER BY id DESC LIMIT 5000""", args)
    rows = cur.fetchall()
    if not rows:
        c.close()
        return []
    # Document frequencies for IDF
    df: Counter[str] = Counter()
    for _, _, _, _, _, toks, _ in rows:
        df.update(set(toks.split()))
    n_docs = len(rows)
    idf = {t: math.log(1 + n_docs / (1 + df[t])) for t in qtoks}
    scored: list[tuple[float, tuple]] = []
    now = int(time.time())
    for row in rows:
        rid, _, _, _, _, toks, ts = row
        dtoks = set(toks.split())
        overlap = qtoks & dtoks
        if not overlap:
            continue
        relevance = sum(idf.get(t, 0) for t in overlap)
        recency = math.exp(-(now - ts) / (60 * 60 * 24 * 30))  # 30-day half-life
        scored.append((relevance * (0.5 + recency), row))
    scored.sort(key=lambda x: -x[0])
    top = scored[:k]
    if top:
        ids = [str(r[1][0]) for r in top]
        c.execute(f"UPDATE lessons SET score = score + 1 WHERE id IN ({','.join(ids)})")
    c.close()
    return [{
        "task": r[1][1], "error": r[1][2], "reflection": r[1][3],
        "fix": r[1][4], "score": round(r[0], 3),
    } for r in top]


def stats() -> dict:
    c = _db()
    cur = c.execute("""SELECT domain, COUNT(*), SUM(score)
                       FROM lessons GROUP BY domain ORDER BY 2 DESC""")
    by_domain = [{"domain": d, "count": n, "score_sum": s or 0}
                 for d, n, s in cur]
    cur = c.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM lessons")
    n, mn, mx = cur.fetchone()
    c.close()
    return {"total": n, "earliest": mn, "latest": mx, "by_domain": by_domain}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif cmd == "retrieve":
        task = sys.argv[2]
        dom = sys.argv[3] if len(sys.argv) > 3 else None
        k = int(sys.argv[4]) if len(sys.argv) > 4 else 3
        print(json.dumps(retrieve_similar(task, dom, k), indent=2,
                         ensure_ascii=False))
    elif cmd == "store":
        # echo '{"task":"...","attempt":"...","error":"...","reflection":"...","fix":"...","domain":"..."}' | python3 reflexion-store.py store
        d = json.load(sys.stdin)
        rid = store(d["task"], d["attempt"], d["error"], d["reflection"],
                    d["fix"], d["domain"])
        print(json.dumps({"id": rid}))
    else:
        print(f"unknown: {cmd}", file=sys.stderr)
        sys.exit(1)
