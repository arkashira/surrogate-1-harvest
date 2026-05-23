-- Shared context / knowledge / memory store for axentx 3-host topology
-- (GCP + Kam1 + Kam2 — and any future host).
--
-- Design:
--   shared_kv: simple JSONB key-value, mainly for operator preferences
--              and persistent LLM persona/system prompts.
--   shared_memory: append-only event log of lessons / mistakes / fixes
--                  (every host writes, every host reads).
--   shared_knowledge: chunked patterns / skills / docs — the daemons'
--              equivalent of the Obsidian Vault on Mac.
--
-- All tables are accessible to the SUPABASE_SECRET_KEY (service-role) so
-- every daemon on every host shares the same view.
--
-- Apply with: psql "$SUPABASE_DB_URL" -f shared-context-schema.sql
-- OR via Supabase SQL editor.

-- ── 1. Key-value (operator preferences, system-prompt overrides) ─────────
CREATE TABLE IF NOT EXISTS shared_kv (
    k          TEXT PRIMARY KEY,
    v          JSONB NOT NULL,
    updated_by TEXT,                                -- host that last wrote
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Helper RPC: upsert a key
CREATE OR REPLACE FUNCTION shared_kv_set(p_k TEXT, p_v JSONB, p_who TEXT)
RETURNS VOID LANGUAGE SQL AS $$
    INSERT INTO shared_kv (k, v, updated_by, updated_at)
    VALUES (p_k, p_v, p_who, NOW())
    ON CONFLICT (k) DO UPDATE
        SET v = EXCLUDED.v,
            updated_by = EXCLUDED.updated_by,
            updated_at = NOW();
$$;

-- ── 2. Append-only memory (lessons learned per host) ────────────────────
CREATE TABLE IF NOT EXISTS shared_memory (
    id         BIGSERIAL PRIMARY KEY,
    host       TEXT NOT NULL,                       -- 'gcp' | 'kam1' | 'kam2'
    actor      TEXT NOT NULL,                       -- daemon name
    kind       TEXT NOT NULL,                       -- 'lesson' | 'fix' | 'pref'
    title      TEXT NOT NULL,
    body       TEXT,
    tags       TEXT[],
    payload    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shared_memory_kind_created
    ON shared_memory (kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shared_memory_tags
    ON shared_memory USING GIN (tags);

-- ── 3. Knowledge base (patterns / skills / facts the daemons consult) ───
CREATE TABLE IF NOT EXISTS shared_knowledge (
    slug       TEXT PRIMARY KEY,                    -- 'pattern/circuit-breaker'
    category   TEXT NOT NULL,                       -- 'pattern' | 'skill' | 'doc'
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,                       -- markdown
    metadata   JSONB,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shared_knowledge_category
    ON shared_knowledge (category, updated_at DESC);

-- Free-text search via tsvector (Postgres native, no extension needed)
CREATE INDEX IF NOT EXISTS idx_shared_knowledge_fts
    ON shared_knowledge USING GIN (to_tsvector('english', title || ' ' || body));

-- Helper RPC: upsert a knowledge entry
CREATE OR REPLACE FUNCTION shared_knowledge_set(
    p_slug TEXT, p_category TEXT, p_title TEXT,
    p_body TEXT, p_metadata JSONB, p_who TEXT
) RETURNS VOID LANGUAGE SQL AS $$
    INSERT INTO shared_knowledge
        (slug, category, title, body, metadata, updated_by, updated_at)
    VALUES (p_slug, p_category, p_title, p_body, p_metadata, p_who, NOW())
    ON CONFLICT (slug) DO UPDATE
        SET category = EXCLUDED.category,
            title = EXCLUDED.title,
            body = EXCLUDED.body,
            metadata = EXCLUDED.metadata,
            updated_by = EXCLUDED.updated_by,
            updated_at = NOW();
$$;

-- ── 4. RLS — service-key writes, anon reads ─────────────────────────────
ALTER TABLE shared_kv         ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared_memory     ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared_knowledge  ENABLE ROW LEVEL SECURITY;

-- Service role can do everything
CREATE POLICY shared_kv_service ON shared_kv
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY shared_memory_service ON shared_memory
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY shared_knowledge_service ON shared_knowledge
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Anon role can read but not write (keeps the LLM-call path simple but safe)
CREATE POLICY shared_kv_read ON shared_kv
    FOR SELECT TO anon USING (true);
CREATE POLICY shared_memory_read ON shared_memory
    FOR SELECT TO anon USING (true);
CREATE POLICY shared_knowledge_read ON shared_knowledge
    FOR SELECT TO anon USING (true);
