-- =============================================================================
-- Lucifer AI — PostgreSQL / TimescaleDB Initial Migration
-- Creates: telemetry_events hypertable, token_budgets, spend_tracking,
--          oauth_tokens, audit_log, agent_manifests
-- =============================================================================

-- ── Extensions ───────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- pgvector registers its extension under the name "vector" (control file
-- vector.control); "pgvector" is not a valid extension name.
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "timescaledb" CASCADE;

-- ── Enums ────────────────────────────────────────────────────────────────────
CREATE TYPE data_classification AS ENUM ('public', 'standard', 'restricted', 'secret');
CREATE TYPE risk_tier AS ENUM ('low', 'medium', 'high', 'critical');
CREATE TYPE agent_status AS ENUM ('success', 'partial', 'escalate', 'error');
CREATE TYPE surface AS ENUM ('watch', 'mobile', 'desktop', 'master', 'cli', 'web');

-- ── Telemetry Events (TimescaleDB Hypertable) ────────────────────────────────
CREATE TABLE telemetry_events (
    id              UUID DEFAULT uuid_generate_v4(),
    event_type      TEXT NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    device_id       TEXT NOT NULL,
    agent_id        TEXT,
    trace_id        TEXT NOT NULL,
    span_id         TEXT NOT NULL,
    metrics         JSONB NOT NULL DEFAULT '{}',
    metadata        JSONB NOT NULL DEFAULT '{}',
    cost_usd        NUMERIC(12, 8),
    error           BOOLEAN NOT NULL DEFAULT FALSE,
    error_code      TEXT,
    PRIMARY KEY (id, timestamp)
);

-- Convert to TimescaleDB hypertable partitioned by timestamp
SELECT create_hypertable('telemetry_events', 'timestamp', chunk_time_interval => INTERVAL '1 day');

-- Enable compression (columnstore) on the hypertable. Required since
-- TimescaleDB 2.18 before add_compression_policy can attach — older releases
-- enabled it implicitly. Segment by agent_id, order by time for query locality.
ALTER TABLE telemetry_events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'agent_id',
    timescaledb.compress_orderby = 'timestamp DESC'
);

-- Compression policy: compress chunks older than 30 days
SELECT add_compression_policy('telemetry_events', INTERVAL '30 days');

-- Retention policy: drop raw events older than 90 days
SELECT add_retention_policy('telemetry_events', INTERVAL '90 days');

-- Indexes
CREATE INDEX idx_telemetry_agent_id ON telemetry_events (agent_id, timestamp DESC);
CREATE INDEX idx_telemetry_device_id ON telemetry_events (device_id, timestamp DESC);
CREATE INDEX idx_telemetry_event_type ON telemetry_events (event_type, timestamp DESC);
CREATE INDEX idx_telemetry_error ON telemetry_events (error, timestamp DESC) WHERE error = TRUE;

-- ── Token Budget Tracking ─────────────────────────────────────────────────────
CREATE TABLE agent_budgets (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id        TEXT NOT NULL UNIQUE,
    daily_limit_usd NUMERIC(10, 4) NOT NULL,
    monthly_limit_usd NUMERIC(10, 4),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed default budgets
INSERT INTO agent_budgets (agent_id, daily_limit_usd, monthly_limit_usd) VALUES
    ('financial-agent',  2.00,  50.00),
    ('health-agent',     1.00,  25.00),
    ('coding-agent',     3.00,  75.00),
    ('personal-agent',   2.00,  50.00),
    ('research-agent',   2.00,  50.00),
    ('librarian-agent',  1.00,  25.00),
    ('news-agent',       0.50,  10.00);

-- Daily spend counter (reset nightly via cron)
CREATE TABLE daily_spend (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id        TEXT NOT NULL,
    date            DATE NOT NULL DEFAULT CURRENT_DATE,
    spend_usd       NUMERIC(10, 6) NOT NULL DEFAULT 0,
    request_count   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (agent_id, date)
);

CREATE INDEX idx_daily_spend_agent_date ON daily_spend (agent_id, date);

-- ── OAuth Token Store ─────────────────────────────────────────────────────────
CREATE TABLE oauth_tokens (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider        TEXT NOT NULL,             -- "google", "github", "notion", "slack"
    user_id         TEXT NOT NULL,
    access_token    BYTEA NOT NULL,            -- AES-256-GCM encrypted
    refresh_token   BYTEA,                     -- AES-256-GCM encrypted
    expires_at      TIMESTAMPTZ,
    scopes          TEXT[] NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider, user_id)
);

CREATE INDEX idx_oauth_tokens_provider_user ON oauth_tokens (provider, user_id);

-- ── Audit Log (HMAC Chain — append-only) ─────────────────────────────────────
CREATE TABLE audit_log (
    sequence        BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type      TEXT NOT NULL,             -- "agent.execute", "tool.call", "memory.write"
    agent_id        TEXT,
    device_id       TEXT,
    resource        TEXT,                      -- affected resource identifier
    action          TEXT NOT NULL,
    payload_hash    TEXT NOT NULL,             -- SHA-256 of event payload
    prev_hmac       TEXT NOT NULL,             -- HMAC of previous record
    chain_hmac      TEXT NOT NULL,             -- HMAC(prev_hmac || payload_hash)
    metadata        JSONB NOT NULL DEFAULT '{}'
);

-- Audit log is append-only: no UPDATE or DELETE allowed (enforced by row-level policy)
CREATE RULE audit_log_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE audit_log_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;

-- ── Artifact Registry ─────────────────────────────────────────────────────────
CREATE TABLE artifacts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content_hash    TEXT NOT NULL UNIQUE,       -- SHA-256 of content
    title           TEXT NOT NULL,
    mime_type       TEXT NOT NULL,
    classification  data_classification NOT NULL DEFAULT 'standard',
    storage_path    TEXT NOT NULL,              -- MinIO path
    size_bytes      BIGINT NOT NULL,
    created_by      TEXT NOT NULL,              -- agent_id
    embedding       VECTOR(1536),               -- for semantic artifact search
    metadata        JSONB NOT NULL DEFAULT '{}',
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_artifacts_content_hash ON artifacts (content_hash);
CREATE INDEX idx_artifacts_created_by ON artifacts (created_by, created_at DESC);
CREATE INDEX idx_artifacts_embedding ON artifacts USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ── Device Registry ───────────────────────────────────────────────────────────
CREATE TABLE devices (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    device_id       TEXT NOT NULL UNIQUE,       -- hashed device fingerprint
    device_type     TEXT NOT NULL,             -- "desktop-mac", "desktop-win", "ios", "android", "watch"
    name            TEXT,
    is_revoked      BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at      TIMESTAMPTZ,
    last_seen_at    TIMESTAMPTZ,
    app_version     TEXT,
    os_version      TEXT,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_devices_device_id ON devices (device_id);
CREATE INDEX idx_devices_active ON devices (is_revoked, last_seen_at DESC) WHERE is_revoked = FALSE;

-- ── Refresh Token Store ───────────────────────────────────────────────────────
CREATE TABLE refresh_tokens (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    device_id       TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,       -- SHA-256 of raw token
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,               -- set on single-use rotation
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_refresh_tokens_device ON refresh_tokens (device_id, expires_at DESC);

-- ── Query Log (real-traffic capture for model-upgrade replay) ─────────────────
CREATE TABLE query_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    prompt          TEXT NOT NULL,
    response        TEXT NOT NULL,
    agent_id        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_query_log_created ON query_log (created_at DESC);

-- ── Trigger: updated_at auto-update ──────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_agent_budgets_updated_at
  BEFORE UPDATE ON agent_budgets
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_oauth_tokens_updated_at
  BEFORE UPDATE ON oauth_tokens
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
