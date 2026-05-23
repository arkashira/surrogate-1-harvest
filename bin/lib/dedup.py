"""
Central dedup hash store — single source of truth for "have we seen this pair?"

Every writer (dataset-enrich, GitHub crawler, agentic crawler, orchestrate,
threat-intel, SRE postmortem, synthetic-data) MUST call DedupStore.is_new()
before appending to training-pairs.jsonl.

Hash: md5(prompt[:500])[:16] — 64-bit collision space, ~1 in 10^19 false-dup
rate at our scale. Stored in ~/.surrogate/state/dedup.db (SQLite, thread-safe).

Usage:
    from lib.dedup import DedupStore
    if DedupStore.is_new(prompt, source="github-crawl-pr"):
        write_pair(...)
    # else skip
"""
from __future__ import annotations
import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

DB_PATH = Path.home() / ".surrogate/state/dedup.db"


class DedupStore:
    _lock = threading.Lock()
    _conn: sqlite3.Connection | None = None

    @classmethod
    def _connection(cls) -> sqlite3.Connection:
        if cls._conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Auto-recover from corruption (16 parallel shards can corrupt SQLite)
            for attempt in range(3):
                try:
                    c = sqlite3.connect(str(DB_PATH), check_same_thread=False,
                                         timeout=30, isolation_level=None)
                    c.execute("PRAGMA journal_mode=WAL")
                    c.execute("PRAGMA synchronous=NORMAL")
                    c.execute("PRAGMA busy_timeout=30000")  # 30s wait on lock
                    c.execute("PRAGMA wal_autocheckpoint=1000")
                    c.executescript("""
                        CREATE TABLE IF NOT EXISTS seen_hashes (
                            hash    TEXT PRIMARY KEY,
                            source  TEXT NOT NULL,
                            ts      INTEGER NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS idx_seen_source ON seen_hashes(source);
                        CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen_hashes(ts);
                    """)
                    # Smoke-test the table
                    c.execute("SELECT 1 FROM seen_hashes LIMIT 1").fetchall()
                    cls._conn = c
                    break
                except sqlite3.DatabaseError as e:
                    if "malformed" in str(e).lower() or "corrupt" in str(e).lower():
                        # Backup + reset corrupted DB
                        import time as _t
                        backup = DB_PATH.with_suffix(f".corrupt-{int(_t.time())}.bak")
                        try:
                            DB_PATH.rename(backup)
                            for ext in ("-wal", "-shm"):
                                p = DB_PATH.with_suffix(DB_PATH.suffix + ext)
                                if p.exists():
                                    p.unlink()
                        except Exception:
                            pass
                        if attempt < 2:
                            continue
                    raise
        return cls._conn

    @classmethod
    def hash_key(cls, prompt: str) -> str:
        return hashlib.md5(prompt[:500].encode("utf-8", errors="ignore")).hexdigest()[:16]

    @classmethod
    def _force_reset(cls) -> None:
        """Backup the corrupt DB and clear the cached connection so the next
        _connection() call rebuilds from scratch. Caller must hold cls._lock."""
        if cls._conn is not None:
            try:
                cls._conn.close()
            except Exception:
                pass
            cls._conn = None
        try:
            if DB_PATH.exists():
                backup = DB_PATH.with_suffix(f".corrupt-{int(time.time())}.bak")
                DB_PATH.rename(backup)
            for ext in ("-wal", "-shm"):
                p = Path(str(DB_PATH) + ext)
                if p.exists():
                    p.unlink()
        except Exception:
            pass

    @staticmethod
    def _is_corruption(e: Exception) -> bool:
        msg = str(e).lower()
        return "malformed" in msg or "corrupt" in msg or "not a database" in msg

    @staticmethod
    def _is_transient(e: Exception) -> bool:
        """Disk I/O contention or lock pile-up under 16 parallel writers.
        Caller should backoff + retry, NOT wipe the DB."""
        msg = str(e).lower()
        return ("disk i/o error" in msg or "database is locked" in msg
                or "cannot start a transaction" in msg)

    @classmethod
    def is_new(cls, prompt: str, source: str = "unknown") -> bool:
        """Atomic check-and-insert. Returns True if hash newly added (writer should
        emit the pair); False if already seen (writer should skip).
        Resilient against:
          - hard corruption -> reset DB once, retry
          - transient I/O / lock contention -> backoff + retry up to 3x"""
        if not prompt:
            return False
        h = cls.hash_key(prompt)
        for attempt in range(4):
            try:
                with cls._lock:
                    con = cls._connection()
                    cur = con.execute(
                        "INSERT OR IGNORE INTO seen_hashes (hash, source, ts) VALUES (?, ?, ?)",
                        (h, source, int(time.time())),
                    )
                    con.commit()
                    return cur.rowcount > 0
            except sqlite3.DatabaseError as e:
                if cls._is_corruption(e) and attempt == 0:
                    with cls._lock:
                        cls._force_reset()
                    continue
                if cls._is_transient(e) and attempt < 3:
                    time.sleep(0.4 * (2 ** attempt))  # 0.4s, 0.8s, 1.6s backoff
                    continue
                # Last resort: don't crash the caller — best to skip than lose
                # the whole batch over a single retry-exhaustion.
                return True  # treat as new; worst case is one duplicate

    @classmethod
    def bulk_seen(cls, prompts: Iterable[str], source: str = "bootstrap") -> int:
        """Mark a batch of prompts as seen. Returns count newly added.
        Same resilience model as is_new()."""
        rows = [(cls.hash_key(p), source, int(time.time())) for p in prompts if p]
        if not rows:
            return 0
        for attempt in range(4):
            try:
                with cls._lock:
                    con = cls._connection()
                    before = con.execute("SELECT COUNT(*) FROM seen_hashes").fetchone()[0]
                    con.executemany(
                        "INSERT OR IGNORE INTO seen_hashes (hash, source, ts) VALUES (?, ?, ?)",
                        rows,
                    )
                    con.commit()
                    after = con.execute("SELECT COUNT(*) FROM seen_hashes").fetchone()[0]
                    return after - before
            except sqlite3.DatabaseError as e:
                if cls._is_corruption(e) and attempt == 0:
                    with cls._lock:
                        cls._force_reset()
                    continue
                if cls._is_transient(e) and attempt < 3:
                    time.sleep(0.4 * (2 ** attempt))
                    continue
                return 0

    @classmethod
    def stats(cls) -> dict:
        """Return DB stats. Safe against corruption — resets and returns empty
        stats rather than crashing the caller (callers like dataset-enrich
        treat stats as diagnostic, not load-bearing)."""
        for attempt in range(2):
            try:
                with cls._lock:
                    con = cls._connection()
                    total = con.execute("SELECT COUNT(*) FROM seen_hashes").fetchone()[0]
                    by_source = dict(con.execute(
                        "SELECT source, COUNT(*) FROM seen_hashes GROUP BY source ORDER BY 2 DESC LIMIT 20"
                    ).fetchall())
                    mn, mx = con.execute("SELECT MIN(ts), MAX(ts) FROM seen_hashes").fetchone()
                return {"total": total, "by_source": by_source, "first_ts": mn, "latest_ts": mx}
            except sqlite3.DatabaseError as e:
                if cls._is_corruption(e) and attempt == 0:
                    with cls._lock:
                        cls._force_reset()
                    continue
                if cls._is_transient(e) and attempt < 1:
                    time.sleep(0.5)
                    continue
                # Last-resort: never let stats() crash a caller
                return {"total": 0, "by_source": {}, "first_ts": None, "latest_ts": None, "error": str(e)}
        return {"total": 0, "by_source": {}, "first_ts": None, "latest_ts": None}


def write_pair_dedup(record: dict, output_path: Path | str, prompt: str | None = None) -> bool:
    """Convenience helper: only append record if its prompt is new.
    Returns True if written, False if skipped as duplicate.
    """
    p = prompt or record.get("prompt") or record.get("instruction") or ""
    if not p:
        return False
    src = record.get("source", "unknown")
    if not DedupStore.is_new(p, src):
        return False
    import json as _json
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a") as f:
        f.write(_json.dumps(record, ensure_ascii=False) + "\n")
    return True


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(DedupStore.stats(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "bootstrap":
        # Read jsonl from stdin, mark all prompts as seen
        added = 0
        prompts = []
        src = sys.argv[2] if len(sys.argv) > 2 else "bootstrap"
        for line in sys.stdin:
            try:
                d = json.loads(line)
                p = d.get("prompt") or d.get("instruction")
                if p: prompts.append(p)
                if len(prompts) >= 5000:
                    added += DedupStore.bulk_seen(prompts, src)
                    prompts = []
            except: pass
        if prompts:
            added += DedupStore.bulk_seen(prompts, src)
        print(f"bootstrapped {added} new hashes (source={src})")
    else:
        print("usage: dedup.py stats | bootstrap [source] < input.jsonl")
