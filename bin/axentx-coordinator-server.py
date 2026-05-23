#!/usr/bin/env python3
"""axentx-coordinator — self-hosted shared-state plane.

Replaces Supabase. Backed by SQLite at /opt/surrogate-1-harvest/state/
axentx-coordinator.db. Exposes HTTP on 0.0.0.0:7878 with Bearer-token auth.

Routes (POST JSON body unless noted):
  GET  /health
  POST /queue/push    {id,stage,project,focus,payload}
  POST /queue/claim   {stage,claimer,ttl_sec?}
  POST /queue/advance {id,new_stage?,claimer?}
  POST /queue/release {id}
  POST /queue/depth   {stage}
  POST /kv/get        {k}
  POST /kv/set        {k,v}
  POST /kv/list       {prefix?}
  POST /knowledge/upsert {topic,content,tags?}
  POST /knowledge/get    {topic}
  POST /knowledge/list   {tag?}
  POST /skill/upsert     {name,definition,tags?}
  POST /skill/get        {name}
  POST /skill/list       {}

Same wire format as the CF Worker version. axentx_pipeline.py code path
unchanged — just point XVM_QUEUE_URL at this host.
"""
import http.server
import json
import os
import socketserver
import sqlite3
import sys
import threading
import time
from urllib.parse import urlparse

DB_PATH = "/opt/surrogate-1-harvest/state/axentx-coordinator.db"
PORT = int(os.environ.get("COORDINATOR_PORT", "7878"))
TOKEN = os.environ.get("COORDINATOR_TOKEN", "axentx-coord-2026-shared-secret")

_db_lock = threading.RLock()


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def db_init():
    with _db_lock, db_connect() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_items (
              id TEXT PRIMARY KEY,
              stage TEXT NOT NULL,
              project TEXT,
              focus TEXT,
              payload TEXT NOT NULL,
              claimer TEXT,
              claimed_at INTEGER,
              claim_ttl INTEGER DEFAULT 600,
              created_at INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_stage_claim
              ON pipeline_items(stage, claimer, claimed_at);
            CREATE INDEX IF NOT EXISTS idx_stage_created
              ON pipeline_items(stage, created_at);
            CREATE TABLE IF NOT EXISTS shared_kv (
              k TEXT PRIMARY KEY,
              v TEXT,
              ts INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS knowledge (
              topic TEXT PRIMARY KEY,
              content TEXT NOT NULL,
              tags TEXT,
              ts INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS skills (
              name TEXT PRIMARY KEY,
              definition TEXT NOT NULL,
              tags TEXT,
              ts INTEGER DEFAULT (strftime('%s','now'))
            );
        """)


# ── Route handlers ──────────────────────────────────────────────────────────
def queue_push(b):
    if not b.get("id") or not b.get("stage"):
        return 400, {"error": "id+stage required"}
    with _db_lock, db_connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO pipeline_items "
            "(id, stage, project, focus, payload) VALUES (?, ?, ?, ?, ?)",
            (b["id"], b["stage"], b.get("project", ""),
             b.get("focus", ""), json.dumps(b.get("payload", {}))),
        )
    return 200, {"pushed": b["id"]}


def queue_claim(b):
    stage = b.get("stage")
    claimer = b.get("claimer")
    if not stage or not claimer:
        return 400, {"error": "stage+claimer required"}
    ttl = int(b.get("ttl_sec") or 600)
    now = int(time.time())
    expired = now - ttl
    with _db_lock, db_connect() as c:
        # Atomic: find + claim in single transaction
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT id FROM pipeline_items WHERE stage = ? "
            "AND (claimer IS NULL OR claimed_at IS NULL OR claimed_at < ?) "
            "ORDER BY created_at ASC LIMIT 1",
            (stage, expired),
        ).fetchone()
        if not row:
            c.execute("COMMIT")
            return 200, {}
        item_id = row[0]
        c.execute(
            "UPDATE pipeline_items SET claimer = ?, claimed_at = ?, "
            "claim_ttl = ? WHERE id = ? "
            "AND (claimer IS NULL OR claimed_at IS NULL OR claimed_at < ?)",
            (claimer, now, ttl, item_id, expired),
        )
        changes = c.total_changes  # cumulative; track delta
        row2 = c.execute(
            "SELECT id, stage, project, focus, payload FROM pipeline_items "
            "WHERE id = ?", (item_id,),
        ).fetchone()
        c.execute("COMMIT")
        if not row2:
            return 200, {}
        return 200, {
            "id": row2[0], "stage": row2[1], "project": row2[2],
            "focus": row2[3], "payload": json.loads(row2[4] or "{}"),
        }


def queue_advance(b):
    item_id = b.get("id")
    if not item_id:
        return 400, {"error": "id required"}
    with _db_lock, db_connect() as c:
        if b.get("new_stage"):
            c.execute(
                "UPDATE pipeline_items SET stage = ?, claimer = NULL, "
                "claimed_at = NULL WHERE id = ?",
                (b["new_stage"], item_id),
            )
        else:
            c.execute("DELETE FROM pipeline_items WHERE id = ?", (item_id,))
    return 200, {"advanced": item_id}


def queue_release(b):
    item_id = b.get("id")
    if not item_id:
        return 400, {"error": "id required"}
    with _db_lock, db_connect() as c:
        c.execute(
            "UPDATE pipeline_items SET claimer = NULL, claimed_at = NULL "
            "WHERE id = ?", (item_id,),
        )
    return 200, {"released": item_id}


def queue_depth(b):
    stage = b.get("stage")
    if not stage:
        return 400, {"error": "stage required"}
    expired = int(time.time()) - 600
    with _db_lock, db_connect() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM pipeline_items WHERE stage = ? "
            "AND (claimer IS NULL OR claimed_at < ?)",
            (stage, expired),
        ).fetchone()[0]
    return 200, {"stage": stage, "depth": n}


def kv_get(b):
    k = b.get("k")
    with _db_lock, db_connect() as c:
        row = c.execute("SELECT v FROM shared_kv WHERE k = ?", (k,)).fetchone()
    return 200, {"k": k, "v": json.loads(row[0]) if row else None}


def kv_set(b):
    with _db_lock, db_connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO shared_kv (k, v, ts) "
            "VALUES (?, ?, strftime('%s','now'))",
            (b.get("k"), json.dumps(b.get("v"))),
        )
    return 200, {"set": b.get("k")}


def kv_list(b):
    prefix = (b.get("prefix") or "") + "%"
    with _db_lock, db_connect() as c:
        rows = c.execute(
            "SELECT k, ts FROM shared_kv WHERE k LIKE ? "
            "ORDER BY ts DESC LIMIT 200", (prefix,),
        ).fetchall()
    return 200, {"keys": [{"k": r[0], "ts": r[1]} for r in rows]}


def knowledge_upsert(b):
    if not b.get("topic") or not b.get("content"):
        return 400, {"error": "topic+content required"}
    with _db_lock, db_connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO knowledge (topic, content, tags, ts) "
            "VALUES (?, ?, ?, strftime('%s','now'))",
            (b["topic"], b["content"], ",".join(b.get("tags", []))),
        )
    return 200, {"upserted": b["topic"]}


def knowledge_get(b):
    with _db_lock, db_connect() as c:
        row = c.execute(
            "SELECT topic, content, tags, ts FROM knowledge WHERE topic = ?",
            (b.get("topic"),),
        ).fetchone()
    if not row:
        return 200, {"topic": b.get("topic"), "content": None}
    return 200, {
        "topic": row[0], "content": row[1],
        "tags": row[2].split(",") if row[2] else [], "ts": row[3],
    }


def knowledge_list(b):
    tag = b.get("tag")
    with _db_lock, db_connect() as c:
        if tag:
            rows = c.execute(
                "SELECT topic, tags, ts FROM knowledge WHERE tags LIKE ? "
                "ORDER BY ts DESC LIMIT 200", ("%" + tag + "%",),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT topic, tags, ts FROM knowledge ORDER BY ts DESC LIMIT 200"
            ).fetchall()
    return 200, {
        "items": [
            {"topic": r[0], "tags": r[1].split(",") if r[1] else [], "ts": r[2]}
            for r in rows
        ]
    }


def skill_upsert(b):
    if not b.get("name") or not b.get("definition"):
        return 400, {"error": "name+definition required"}
    with _db_lock, db_connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO skills (name, definition, tags, ts) "
            "VALUES (?, ?, ?, strftime('%s','now'))",
            (b["name"], b["definition"], ",".join(b.get("tags", []))),
        )
    return 200, {"upserted": b["name"]}


def skill_get(b):
    with _db_lock, db_connect() as c:
        row = c.execute(
            "SELECT name, definition, tags, ts FROM skills WHERE name = ?",
            (b.get("name"),),
        ).fetchone()
    if not row:
        return 200, {"name": b.get("name"), "definition": None}
    return 200, {
        "name": row[0], "definition": row[1],
        "tags": row[2].split(",") if row[2] else [], "ts": row[3],
    }


def skill_list(b):
    with _db_lock, db_connect() as c:
        rows = c.execute(
            "SELECT name, tags, ts FROM skills ORDER BY ts DESC LIMIT 200"
        ).fetchall()
    return 200, {
        "items": [
            {"name": r[0], "tags": r[1].split(",") if r[1] else [], "ts": r[2]}
            for r in rows
        ]
    }


ROUTES = {
    "/queue/push": queue_push,
    "/queue/claim": queue_claim,
    "/queue/advance": queue_advance,
    "/queue/release": queue_release,
    "/queue/depth": queue_depth,
    "/kv/get": kv_get,
    "/kv/set": kv_set,
    "/kv/list": kv_list,
    "/knowledge/upsert": knowledge_upsert,
    "/knowledge/get": knowledge_get,
    "/knowledge/list": knowledge_list,
    "/skill/upsert": skill_upsert,
    "/skill/get": skill_get,
    "/skill/list": skill_list,
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter
        return

    def _check_auth(self):
        if TOKEN and TOKEN != "":
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {TOKEN}":
                self.send_error(401, "auth required")
                return False
        return True

    def _respond(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._respond(200, {
                "ok": True, "service": "axentx-coordinator", "ts": int(time.time())
            })
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._check_auth():
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._respond(400, {"error": f"invalid json: {e}"})
            return
        handler = ROUTES.get(path)
        if not handler:
            self.send_error(404)
            return
        try:
            code, resp = handler(body)
            self._respond(code, resp)
        except Exception as e:
            self._respond(500, {"error": f"handler-error: {e}"})


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    db_init()
    print(f"axentx-coordinator on 0.0.0.0:{PORT} (db={DB_PATH})", flush=True)
    with ThreadingServer(("0.0.0.0", PORT), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
