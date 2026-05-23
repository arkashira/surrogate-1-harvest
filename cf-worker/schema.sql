-- D1 schema for surrogate-1-cursor (replaces HF Space's filesystem cursor state)
-- Apply via: wrangler d1 execute surrogate-1-cursor --file=schema.sql
-- Or via API: POST /accounts/{acct}/d1/database/{uuid}/query

CREATE TABLE IF NOT EXISTS cursors (
    dataset_id  TEXT PRIMARY KEY,
    offset      INTEGER NOT NULL DEFAULT 0,
    total       INTEGER,
    last_batch  TEXT,
    updated_at  INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS datasets (
    slug          TEXT PRIMARY KEY,
    hf_id         TEXT NOT NULL,
    schema        TEXT,
    license       TEXT,
    score         REAL DEFAULT 0.5,
    cap           INTEGER DEFAULT 50000,
    downloads     INTEGER DEFAULT 0,
    discovered_ts INTEGER DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_datasets_score ON datasets(score DESC);
CREATE INDEX IF NOT EXISTS idx_cursors_updated ON cursors(updated_at);

-- Round 1 additions (2026-05-02): exhaustion tracking + audit + metrics
ALTER TABLE cursors ADD COLUMN exhausted INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    dataset_id  TEXT,
    meta        TEXT,
    ts          INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);

CREATE TABLE IF NOT EXISTS metrics (
    key  TEXT PRIMARY KEY,
    n    INTEGER NOT NULL DEFAULT 0
);

-- Round 3 (2026-05-02) — CF expansion: scheduled health pings
CREATE TABLE IF NOT EXISTS space_health (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    space_id    TEXT NOT NULL,
    http_code   INTEGER,
    latency_ms  INTEGER,
    ts          INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_space_health_ts ON space_health(ts DESC);

-- Round 4 (2026-05-02) — feature batch
-- #42 distributed tracing: trace_id on audit_log
ALTER TABLE audit_log ADD COLUMN trace_id TEXT;
CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_log(trace_id);

-- #36 canary results
CREATE TABLE IF NOT EXISTS canary_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT,
    success     INTEGER NOT NULL,
    latency_ms  INTEGER,
    errors      TEXT,
    ts          INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_canary_ts ON canary_runs(ts DESC);

-- #99 pricing A/B click tracking
CREATE TABLE IF NOT EXISTS experiment_clicks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_key  TEXT NOT NULL,
    variant         TEXT NOT NULL,
    target          TEXT,
    ip_hash         TEXT,
    ts              INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_exp_clicks_key_ts ON experiment_clicks(experiment_key, ts DESC);

-- #77 audit log immutability — block UPDATE and DELETE on audit_log
DROP TRIGGER IF EXISTS audit_log_no_update;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only');
END;

DROP TRIGGER IF EXISTS audit_log_no_delete;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only');
END;

-- 2026-05-08 — Supabase escape-hatch tables (kv, memory, knowledge)

CREATE TABLE IF NOT EXISTS kv_store (
    k    TEXT PRIMARY KEY,
    v    TEXT NOT NULL,
    who  TEXT,
    ts   INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_kv_store_ts ON kv_store(ts DESC);

CREATE TABLE IF NOT EXISTS shared_memory (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    host    TEXT,
    actor   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    title   TEXT,
    body    TEXT,
    tags    TEXT,
    payload TEXT,
    ts      INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_shared_memory_actor_kind_ts
    ON shared_memory(actor, kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_shared_memory_ts ON shared_memory(ts DESC);

CREATE TABLE IF NOT EXISTS shared_knowledge (
    k       TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    tags    TEXT,
    ts      INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_shared_knowledge_ts ON shared_knowledge(ts DESC);

-- agent heartbeat (replaces KV layer that hit 1000-writes/day free quota)
-- Each daemon UPSERTs its row every HEARTBEAT_SEC; /dash/agents queries
-- by last_seen > now-300s to render the live grid.
CREATE TABLE IF NOT EXISTS agent_status (
    agent       TEXT PRIMARY KEY,
    host        TEXT,
    pid         INTEGER,
    state       TEXT,
    task        TEXT,
    cycle_n     INTEGER DEFAULT 0,
    last_error  TEXT,
    started_at  TEXT,
    last_seen   INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_agent_status_last_seen
    ON agent_status(last_seen DESC);
