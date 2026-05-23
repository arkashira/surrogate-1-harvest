"""Surrogate-1 v2 — Voyager-style skill library.

Validated code/config snippets, auto-promoted as the model uses them
successfully. Inspired by Wang et al. 2023 (Voyager — Minecraft).

Skill = (name, code, description, tags, success_count, failure_count,
         promoted, last_used). Promoted skills (success ≥ 3) ship as
retrieval context at inference.

DB: ~/.surrogate/state/skills.db
Export: ~/.surrogate/data/v2/skills-promoted.jsonl (for training)

Used by:
  - tool-trace-collector.py (extracts candidate skills from successful tool runs)
  - self-improve-loop.sh (re-ranks skills weekly)
  - serve-vllm.sh prompt (retrieves top-k by tag at inference)
"""
from __future__ import annotations
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path.home() / ".surrogate/state/skills.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
PROMOTE_THRESHOLD = 3
EXPORT_PATH = Path.home() / ".surrogate/data/v2/skills-promoted.jsonl"
TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=30,
                        check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        code TEXT,
        description TEXT,
        tags TEXT,                -- comma-separated
        success_count INTEGER DEFAULT 0,
        failure_count INTEGER DEFAULT 0,
        promoted INTEGER DEFAULT 0,
        created_at INTEGER,
        last_used INTEGER
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_skills_promoted ON skills(promoted, success_count DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_skills_tags ON skills(tags)")
    return c


def add(name: str, code: str, description: str,
        tags: list[str] | str = "") -> int:
    if isinstance(tags, list):
        tags = ",".join(t.strip().lower() for t in tags if t.strip())
    c = _db()
    now = int(time.time())
    cur = c.execute("""INSERT OR IGNORE INTO skills
                       (name, code, description, tags, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (name, code, description, tags, now))
    rid = cur.lastrowid
    c.close()
    return rid or -1


def record(name: str, success: bool) -> None:
    c = _db()
    now = int(time.time())
    col = "success_count" if success else "failure_count"
    c.execute(f"UPDATE skills SET {col} = {col}+1, last_used=? WHERE name=?",
              (now, name))
    if success:
        c.execute(f"""UPDATE skills SET promoted=1
                      WHERE name=? AND promoted=0 AND success_count >= ?""",
                  (name, PROMOTE_THRESHOLD))
    c.close()


def search(query: str, tags: list[str] | None = None,
           limit: int = 5, only_promoted: bool = True) -> list[dict]:
    qtoks = set(TOKEN_RE.findall(query.lower()))
    c = _db()
    where = ["1=1"]
    args: list = []
    if only_promoted:
        where.append("promoted = 1")
    if tags:
        for t in tags:
            where.append("tags LIKE ?")
            args.append(f"%{t.lower()}%")
    sql = f"""SELECT name, code, description, tags, success_count, failure_count
              FROM skills WHERE {' AND '.join(where)}
              ORDER BY success_count DESC LIMIT 200"""
    rows = c.execute(sql, args).fetchall()
    c.close()
    if not rows:
        return []
    scored: list[tuple[float, tuple]] = []
    for r in rows:
        name, code, desc, tag_str, ok_n, fail_n = r
        haystack = (name + " " + (desc or "") + " " + (tag_str or "")).lower()
        htoks = set(TOKEN_RE.findall(haystack))
        overlap = qtoks & htoks if qtoks else htoks
        if qtoks and not overlap:
            continue
        rel_score = (len(overlap) if qtoks else 1) * 1.0
        confidence = ok_n / max(1, ok_n + fail_n)
        scored.append((rel_score * (0.5 + confidence), r))
    scored.sort(key=lambda x: -x[0])
    return [{
        "name": r[1][0], "code": r[1][1], "description": r[1][2],
        "tags": r[1][3].split(",") if r[1][3] else [],
        "success": r[1][4], "failure": r[1][5],
        "rank_score": round(r[0], 3),
    } for r in scored[:limit]]


def export_jsonl(path: str | Path = EXPORT_PATH) -> int:
    """Dump promoted skills as JSONL for training data inclusion."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = _db()
    rows = c.execute("""SELECT name, code, description, tags, success_count
                        FROM skills WHERE promoted=1
                        ORDER BY success_count DESC""").fetchall()
    c.close()
    n = 0
    with open(p, "w") as f:
        for name, code, desc, tag_str, ok_n in rows:
            tags = tag_str.split(",") if tag_str else []
            prompt = (f"How would you {desc.lower() if desc else name}?"
                      if desc else f"Provide a working snippet for: {name}")
            f.write(json.dumps({
                "prompt": prompt, "response": code,
                "source": "voyager-skill",
                "meta": {"skill": name, "tags": tags, "uses": ok_n},
            }, ensure_ascii=False) + "\n")
            n += 1
    return n


def stats() -> dict:
    c = _db()
    total = c.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    promoted = c.execute("SELECT COUNT(*) FROM skills WHERE promoted=1").fetchone()[0]
    top = c.execute("""SELECT name, success_count, failure_count, tags
                       FROM skills WHERE promoted=1
                       ORDER BY success_count DESC LIMIT 10""").fetchall()
    c.close()
    return {
        "total": total, "promoted": promoted,
        "top": [{"name": n, "ok": o, "fail": f, "tags": t}
                for n, o, f, t in top],
    }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "stats":
        print(json.dumps(stats(), indent=2, ensure_ascii=False))
    elif cmd == "add":
        d = json.load(sys.stdin)
        rid = add(d["name"], d["code"], d.get("description", ""),
                  d.get("tags", []))
        print(json.dumps({"id": rid}))
    elif cmd == "record":
        record(sys.argv[2], sys.argv[3].lower() in ("ok", "true", "1", "success"))
    elif cmd == "search":
        q = sys.argv[2]
        tags = sys.argv[3].split(",") if len(sys.argv) > 3 else None
        k = int(sys.argv[4]) if len(sys.argv) > 4 else 5
        print(json.dumps(search(q, tags, k), indent=2, ensure_ascii=False))
    elif cmd == "export":
        path = sys.argv[2] if len(sys.argv) > 2 else str(EXPORT_PATH)
        n = export_jsonl(path)
        print(json.dumps({"exported": n, "path": path}))
    else:
        print(f"unknown: {cmd}", file=sys.stderr)
        sys.exit(1)
