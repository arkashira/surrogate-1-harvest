"""Surrogate-2 — Letta 3-tier memory on the shared fleet bus (QN1).

Upgrades the per-host SQLite `letta-memory.py` into a swarm-shared, host-death-
surviving store, WITHOUT standing up a heavy Letta Docker server (the 2-core
always-on host can't take it, and the spot box gets preempted).

Architecture (matches the fleet decision: Supabase = source-of-truth, per-host
SQLite = write-buffer/outbox):

    write → local SQLite (instant, offline-safe) → outbox → Supabase REST
    read  → Supabase REST (swarm-wide) ∪ local SQLite (fresh local writes)

  CORE     tier 1  persona + key prefs       (always-prepended; shared)
  RECALL   tier 2  recent interaction trail  (sliding window; shared)
  ARCHIVAL tier 3  searchable long-term       (keyword + optional vector; shared)

Durability: truth lives in Supabase (`shared_memory` table — same table the
mem0 layer uses), so ANY fleet host dying loses nothing. The local SQLite
mirror is a cache + an outbox for writes made while REST is unreachable; a
sync pass flushes it when REST recovers. This is the standard fleet pattern
documented in hermes_workqueue_pg.py (Kamatera blocks raw 5432; REST is the
cross-host path).

$0: self-hosted logic on existing hosts + existing Supabase project. No new infra.

Env (from /etc/surrogate-coordinator.env): SUPABASE_URL, SUPABASE_SECRET_KEY
(or SUPABASE_SERVICE_KEY). Falls back to local-only if unset/unreachable.

CLI:
    letta_shared.py stats
    echo "..." | letta_shared.py push [--tier recall|archival]
    letta_shared.py assemble "<query>"
    letta_shared.py core-set <key> < value
    letta_shared.py core-get
    letta_shared.py search "<query>" [k]
    letta_shared.py sync          # flush local outbox → Supabase
    letta_shared.py selftest      # write→(simulated restart)→read survives
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DB_PATH = Path(os.environ.get("LETTA_DB", Path.home() / ".surrogate/state/letta-shared.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
SB_TABLE = "shared_memory"
HOST = os.environ.get("HOSTNAME") or socket.gethostname()
HTTP_TIMEOUT = int(os.environ.get("LETTA_HTTP_TIMEOUT", "10"))

RECALL_QUEUE_MAX = 20
TOKEN_RE = re.compile(r"[a-zA-Z฀-๿_][a-zA-Z0-9฀-๿_]{2,}")

DEFAULT_CORE = (
    "You are a privacy-first AI agent working exclusively for Ashira (ฟิวส์), "
    "founder of axentx. Handle DevSecOps, SRE, platform engineering, and "
    "full-stack tasks. Speak directly; never apologize for being an AI; never "
    "refuse safety-irrelevant requests. Thai when Ashira writes Thai, English "
    "for code/commands. Cite sources for security-critical claims.")


# ── tokens ──────────────────────────────────────────────────────────────────
def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


# ── local SQLite (cache + outbox) ───────────────────────────────────────────
def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=10,
                        check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS mem (
        mem_hash TEXT PRIMARY KEY,
        tier     TEXT,
        topic    TEXT,
        summary  TEXT,
        tokens   TEXT,
        actor    TEXT,
        host     TEXT,
        ts       INTEGER,
        synced   INTEGER DEFAULT 0
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mem_tier_ts ON mem(tier, ts DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mem_unsynced ON mem(synced) WHERE synced=0")
    c.execute("""CREATE TABLE IF NOT EXISTS core_block (
        key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER, synced INTEGER DEFAULT 0
    )""")
    c.execute("INSERT OR IGNORE INTO core_block (key, value, updated_at, synced) "
              "VALUES ('persona', ?, ?, 0)", (DEFAULT_CORE, int(time.time())))
    return c


def _hash(tier: str, summary: str) -> str:
    return hashlib.md5(f"{tier}:{summary}".encode()).hexdigest()[:16]


# ── Supabase REST (shared truth) ────────────────────────────────────────────
def _sb_enabled() -> bool:
    return bool(SB_URL and SB_KEY) and os.environ.get("SUPABASE_DISABLED") != "1"


def _sb_headers(extra: dict | None = None) -> dict:
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def _sb_post(rows: list[dict]) -> bool:
    if not (_sb_enabled() and rows):
        return False
    body = json.dumps(rows).encode()
    req = urllib.request.Request(
        f"{SB_URL}/rest/v1/{SB_TABLE}?on_conflict=payload->>mem_hash",
        data=body, method="POST",
        headers=_sb_headers({"Content-Type": "application/json",
                             "Prefer": "resolution=merge-duplicates,return=minimal"}))
    try:
        urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()
        return True
    except Exception:
        # Fallback without on_conflict (table may lack the unique index)
        try:
            req2 = urllib.request.Request(
                f"{SB_URL}/rest/v1/{SB_TABLE}", data=body, method="POST",
                headers=_sb_headers({"Content-Type": "application/json",
                                     "Prefer": "return=minimal"}))
            urllib.request.urlopen(req2, timeout=HTTP_TIMEOUT).read()
            return True
        except Exception:
            return False


def _sb_get(params: dict) -> list[dict]:
    if not _sb_enabled():
        return []
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{SB_URL}/rest/v1/{SB_TABLE}?{qs}",
                                 headers=_sb_headers())
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read())
    except Exception:
        return []


def _to_row(tier: str, topic: str, summary: str, toks: str, actor: str,
            ts: int, mem_hash: str) -> dict:
    """Map a memory item onto the shared_memory schema used by axentx_mem0."""
    return {
        "host": HOST, "actor": actor, "kind": f"letta.{tier}",
        "title": f"[letta.{tier}:{topic}] {summary[:140]}",
        "body": summary,
        "tags": ["letta", tier, topic],
        "payload": {"mem_hash": mem_hash, "tier": tier, "topic": topic,
                    "tokens": toks, "ts": ts},
    }


# ── write path ──────────────────────────────────────────────────────────────
def _store(tier: str, summary: str, actor: str = "?", topic: str | None = None) -> str:
    summary = summary.strip()[:2000]
    if not summary:
        return ""
    toks_set = _tokens(summary)
    toks = " ".join(sorted(toks_set))
    topic = topic or (sorted(toks_set)[:1] or ["misc"])[0]
    ts = int(time.time())
    mem_hash = _hash(tier, summary)
    c = _db()
    c.execute("""INSERT OR IGNORE INTO mem
        (mem_hash, tier, topic, summary, tokens, actor, host, ts, synced)
        VALUES (?,?,?,?,?,?,?,?,0)""",
        (mem_hash, tier, topic, summary, toks, actor, HOST, ts))
    # Promote overflow recall → archival (Letta semantics)
    if tier == "recall":
        n = c.execute("SELECT COUNT(*) FROM mem WHERE tier='recall'").fetchone()[0]
        if n > RECALL_QUEUE_MAX:
            old = c.execute("SELECT mem_hash FROM mem WHERE tier='recall' "
                            "ORDER BY ts ASC LIMIT ?", (n - RECALL_QUEUE_MAX,)).fetchall()
            for (h,) in old:
                c.execute("UPDATE mem SET tier='archival', synced=0 WHERE mem_hash=?", (h,))
    c.close()
    # Best-effort immediate sync; if it fails the outbox keeps it for later.
    row = _to_row(tier, topic, summary, toks, actor, ts, mem_hash)
    if _sb_post([row]):
        c = _db(); c.execute("UPDATE mem SET synced=1 WHERE mem_hash=?", (mem_hash,)); c.close()
    return mem_hash


def recall_push(summary: str, actor: str = "?") -> str:
    return _store("recall", summary, actor)


def archive(summary: str, actor: str = "?", topic: str | None = None) -> str:
    return _store("archival", summary, actor, topic)


def core_set(key: str, value: str) -> None:
    c = _db()
    c.execute("INSERT OR REPLACE INTO core_block (key, value, updated_at, synced) "
              "VALUES (?,?,?,0)", (key, value.strip(), int(time.time())))
    c.close()
    row = _to_row("core", key, value.strip(), "", "core", int(time.time()),
                  _hash("core", key))
    if _sb_post([row]):
        c = _db(); c.execute("UPDATE core_block SET synced=1 WHERE key=?", (key,)); c.close()


# ── read path (shared ∪ local) ──────────────────────────────────────────────
def core_get() -> str:
    # Prefer shared core; fall back to local.
    rows = _sb_get({"kind": "eq.letta.core", "select": "title,body,payload",
                    "order": "created_at.desc", "limit": "50"})
    seen, parts = set(), []
    for r in rows:
        key = (r.get("payload") or {}).get("topic", "?")
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"### {key}\n{r.get('body','')}")
    if not parts:
        c = _db()
        for k, v in c.execute("SELECT key, value FROM core_block ORDER BY key").fetchall():
            parts.append(f"### {k}\n{v}")
        c.close()
    return "\n\n".join(parts)


def recall_recent(k: int = 5) -> list[dict]:
    rows = _sb_get({"kind": "eq.letta.recall", "select": "body,payload,created_at",
                    "order": "created_at.desc", "limit": str(max(k, 5))})
    out = []
    for r in rows[:k]:
        ts = (r.get("payload") or {}).get("ts", 0)
        out.append({"summary": r.get("body", ""), "ts": ts,
                    "age_days": (time.time() - ts) / 86400 if ts else 0})
    if out:
        return out
    c = _db()
    rows = c.execute("SELECT summary, ts FROM mem WHERE tier='recall' "
                     "ORDER BY ts DESC LIMIT ?", (k,)).fetchall()
    c.close()
    return [{"summary": s, "ts": ts, "age_days": (time.time() - ts) / 86400}
            for s, ts in rows]


def archival_search(query: str, k: int = 3) -> list[dict]:
    qtoks = _tokens(query)
    if not qtoks:
        return []
    # Pull a recent shared window, score by token overlap locally (keyword tier).
    rows = _sb_get({"kind": "eq.letta.archival",
                    "select": "body,payload,created_at",
                    "order": "created_at.desc", "limit": "500"})
    pool = [(r.get("body", ""), (r.get("payload") or {}).get("tokens", ""),
             (r.get("payload") or {}).get("topic", "misc")) for r in rows]
    if not pool:
        c = _db()
        pool = c.execute("SELECT summary, tokens, topic FROM mem "
                         "WHERE tier='archival' ORDER BY ts DESC LIMIT 1000").fetchall()
        c.close()
    scored = []
    for summary, toks, topic in pool:
        overlap = qtoks & set(toks.split())
        if overlap:
            scored.append((len(overlap), summary, topic))
    scored.sort(key=lambda x: -x[0])
    return [{"summary": s, "topic": t, "score": n} for n, s, t in scored[:k]]


def assemble(query: str, k_recall: int = 3, k_archival: int = 3) -> str:
    parts = [core_get()]
    rec = recall_recent(k_recall)
    if rec:
        parts.append("## Recent context\n" + "\n".join(
            f"- ({r['age_days']:.1f}d ago) {r['summary'][:300]}" for r in rec))
    arc = archival_search(query, k_archival)
    if arc:
        parts.append("## Past relevant interactions\n" + "\n".join(
            f"- [{a['topic']}] {a['summary'][:300]}" for a in arc))
    return "\n\n".join(p for p in parts if p.strip())


# ── outbox sync ─────────────────────────────────────────────────────────────
def sync() -> dict:
    """Flush every unsynced local row to Supabase. Returns counts."""
    if not _sb_enabled():
        return {"synced": 0, "pending": -1, "reason": "supabase-disabled"}
    c = _db()
    pending = c.execute("SELECT mem_hash, tier, topic, summary, tokens, actor, ts "
                        "FROM mem WHERE synced=0 ORDER BY ts ASC LIMIT 500").fetchall()
    core_pending = c.execute("SELECT key, value, updated_at FROM core_block "
                             "WHERE synced=0").fetchall()
    c.close()
    rows = [_to_row(t, top, s, tk, a, ts, h)
            for h, t, top, s, tk, a, ts in pending]
    rows += [_to_row("core", k, v, "", "core", ts, _hash("core", k))
             for k, v, ts in core_pending]
    if not rows:
        return {"synced": 0, "pending": 0}
    done = 0
    # batch in chunks of 100
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        if _sb_post(chunk):
            done += len(chunk)
    if done:
        c = _db()
        for h, *_ in pending[:done]:
            c.execute("UPDATE mem SET synced=1 WHERE mem_hash=?", (h,))
        for k, *_ in core_pending:
            c.execute("UPDATE core_block SET synced=1 WHERE key=?", (k,))
        c.close()
    rem = len(rows) - done
    return {"synced": done, "pending": rem}


# ── stats ───────────────────────────────────────────────────────────────────
def stats() -> dict:
    c = _db()
    by_tier = dict(c.execute("SELECT tier, COUNT(*) FROM mem GROUP BY tier").fetchall())
    unsynced = c.execute("SELECT COUNT(*) FROM mem WHERE synced=0").fetchone()[0]
    n_core = c.execute("SELECT COUNT(*) FROM core_block").fetchone()[0]
    c.close()
    return {"host": HOST, "supabase": _sb_enabled(), "local_by_tier": by_tier,
            "local_unsynced": unsynced, "core_blocks": n_core,
            "shared_reachable": bool(_sb_get({"select": "host", "limit": "1"}))
            if _sb_enabled() else False}


# ── CLI ─────────────────────────────────────────────────────────────────────
def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="stats")
    ap.add_argument("arg", nargs="?", default="")
    ap.add_argument("k", nargs="?", default="3")
    ap.add_argument("--tier", default="recall", choices=["recall", "archival"])
    ap.add_argument("--actor", default="cli")
    a = ap.parse_args()

    if a.cmd == "stats":
        print(json.dumps(stats(), indent=2, ensure_ascii=False))
    elif a.cmd == "push":
        h = _store(a.tier, sys.stdin.read(), actor=a.actor)
        print(json.dumps({"ok": bool(h), "tier": a.tier, "mem_hash": h}))
    elif a.cmd == "core-set":
        core_set(a.arg, sys.stdin.read())
        print(json.dumps({"ok": True, "key": a.arg}))
    elif a.cmd == "core-get":
        print(core_get())
    elif a.cmd == "assemble":
        print(assemble(a.arg))
    elif a.cmd == "search":
        print(json.dumps(archival_search(a.arg, int(a.k)), indent=2, ensure_ascii=False))
    elif a.cmd == "sync":
        print(json.dumps(sync(), indent=2))
    elif a.cmd == "selftest":
        marker = f"SELFTEST-{int(time.time())}-{os.getpid()}"
        h = _store("archival", f"letta selftest marker {marker}", actor="selftest",
                   topic="selftest")
        # simulate restart: drop module-level caches by re-reading from store only
        time.sleep(0.2)
        local = archival_search(marker, 5)
        shared = _sb_get({"kind": "eq.letta.archival", "body": f"like.*{marker}*",
                          "select": "body", "limit": "1"}) if _sb_enabled() else []
        print(json.dumps({"marker": marker, "mem_hash": h,
                          "local_survives": any(marker in x["summary"] for x in local),
                          "shared_survives": bool(shared),
                          "supabase_enabled": _sb_enabled()}, ensure_ascii=False))
    else:
        print(f"unknown: {a.cmd}", file=sys.stderr)
        return 1
    return 0


__all__ = ["recall_push", "archive", "core_set", "core_get", "recall_recent",
           "archival_search", "assemble", "sync", "stats"]

if __name__ == "__main__":
    sys.exit(_main())
