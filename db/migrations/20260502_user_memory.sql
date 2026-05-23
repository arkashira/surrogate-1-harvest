-- 2026-05-02: per-user memory for Surrogate Discord bot.
--
-- User feedback (ฟิวส์):
--   "ต้องจำชื่อ จำไลฟ์สไตล์ จำได้หมด เค้าเป็นใคร เค้าชอบอะไร ไม่ชอบอะไร
--    และมอบคำตอบในแนวที่เค้าสนใจได้ เหมือน recommendation system"
--
-- Two tables:
--   user_profiles  — one row per Discord user; rolled summary + interests
--                    are refreshed by the bot every 10 messages from the
--                    user (LLM extracts interests/style/locale/notes from
--                    the last 30 turns of their history).
--   chat_history   — append-only per-turn log; build_messages() pulls the
--                    last 20 rows for the requesting user_id so multi-turn
--                    context survives bot restarts and channel switches.
--
-- Why per-user (not per-channel):
--   The same user can hop between #general/#dev/DM, but their profile and
--   recent context are theirs alone. Channel-id is recorded only as a
--   provenance hint — never used as the lookup key.

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id              TEXT PRIMARY KEY,
    display_name         TEXT,
    locale               TEXT,                       -- 'th' | 'en' | 'mix'
    style                TEXT,                       -- 'casual' | 'formal' | 'playful' | 'engineer' | 'mixed'
    interests            JSONB DEFAULT '[]'::jsonb,  -- ['coding','music','cooking', ...]
    dislikes             JSONB DEFAULT '[]'::jsonb,
    notes                TEXT,                       -- bot's free-form notes
    summary              TEXT,                       -- 2-3 sentence "who is this person" rolled by LLM
    n_messages           INT DEFAULT 0,
    last_summary_at_msg  INT DEFAULT 0,              -- n_messages snapshot when summary last refreshed
    first_seen           TIMESTAMPTZ DEFAULT NOW(),
    last_seen            TIMESTAMPTZ DEFAULT NOW(),
    last_topic           TEXT
);

CREATE TABLE IF NOT EXISTS chat_history (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    channel_id  TEXT,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_history_user_at
    ON chat_history(user_id, at DESC);

CREATE INDEX IF NOT EXISTS idx_chat_history_at
    ON chat_history(at DESC);

-- Optional: keep chat_history bounded so it never explodes. Trim turns older
-- than 90 days; the rolled summary in user_profiles preserves long-term
-- memory beyond that horizon.
-- (Run as a Supabase scheduled function or just let it grow — postgres
-- handles millions of rows fine, and we can prune later.)

COMMENT ON TABLE user_profiles  IS 'Per-Discord-user profile + LLM-rolled summary; refreshed every 10 msgs';
COMMENT ON TABLE chat_history   IS 'Append-only per-turn log; supplies multi-turn context to bot LLM';
