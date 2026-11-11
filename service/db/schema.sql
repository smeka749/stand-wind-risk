-- Postgres DDL for the Quote Intent table.
-- One row per quote. phase = pipeline stage, status = stage outcome.
-- A stage only starts from the previous stage at status='Success'.
-- Claim: UPDATE ... WHERE phase=? AND status=? AND
--        (lease_expires_at IS NULL OR lease_expires_at < now())
--        ... with SELECT ... FOR UPDATE SKIP LOCKED in a transaction
--        (see PostgresQuoteStore.claim_next in app/store.py).
CREATE TABLE IF NOT EXISTS quotes (
    id UUID PRIMARY KEY,
    address TEXT NOT NULL,
    normalized_address TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'Requested',
    status TEXT NOT NULL DEFAULT 'Created',
    worker_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    retry_count INT NOT NULL DEFAULT 0,
    error TEXT,
    score NUMERIC,
    case_s3_key TEXT,
    field_s3_key TEXT,
    video_s3_key TEXT,
    -- Per-stage timing, measured by the worker's wrapper.
    stage_name TEXT,
    stage_started_at TIMESTAMPTZ,
    stage_ended_at TIMESTAMPTZ,
    stage_duration_seconds NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    phase_started_at TIMESTAMPTZ
);

-- Idempotency: same normalized address reuses the same quote.
CREATE UNIQUE INDEX IF NOT EXISTS quotes_normalized_address_idx
    ON quotes (normalized_address);

-- Worker polling: find the next eligible row for a stage, oldest first.
CREATE INDEX IF NOT EXISTS quotes_poll_idx
    ON quotes (phase, status, created_at);

-- One row per finished stage attempt: history, provenance (which image
-- ran it) and the durations the ETA is estimated from.
CREATE TABLE IF NOT EXISTS quote_events (
    id BIGSERIAL PRIMARY KEY,
    quote_id UUID NOT NULL REFERENCES quotes (id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL,          -- Success | Fail
    worker_id TEXT,
    image_tag TEXT,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_seconds DOUBLE PRECISION,
    error TEXT
);
CREATE INDEX IF NOT EXISTS quote_events_quote_idx ON quote_events (quote_id, id);
CREATE INDEX IF NOT EXISTS quote_events_stage_idx
    ON quote_events (stage, outcome, ended_at DESC);
