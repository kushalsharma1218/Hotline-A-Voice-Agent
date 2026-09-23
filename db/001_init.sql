-- Hotline app schema (PRD §6.3). Idempotent: safe to run more than once.
-- Mounted into /docker-entrypoint-initdb.d, so the postgres image runs it once
-- on first init of an empty volume, as POSTGRES_USER against POSTGRES_DB.
-- n8n keeps its own tables in schema "public" of the same database.

CREATE SCHEMA IF NOT EXISTS hotline;

-- One row per ElevenLabs conversation. PK = idempotency key for webhook retries.
-- Every status change is a guarded update:
--   UPDATE hotline.call_events SET status = $new, updated_at = now()
--   WHERE conversation_id = $id AND status = $expected;
CREATE TABLE IF NOT EXISTS hotline.call_events (
    conversation_id  text PRIMARY KEY,
    agent_id         text NOT NULL,
    status           text NOT NULL CHECK (status IN (
                         'RECEIVED','FORWARDED','FORWARD_FAILED','EXTRACTED',
                         'LOGGED_ONLY','PENDING_APPROVAL','WRITTEN',
                         'REJECTED','EXPIRED','FAILED')),
    transcript_text  text NOT NULL,
    user_turns       int NOT NULL,
    call_duration_s  int,
    caller_phone     text,
    raw_payload      jsonb NOT NULL,
    forward_attempts int NOT NULL DEFAULT 0,
    last_error       text,              -- "<node name>: <message>" (contracts.md)
    received_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- PRD uses an unnamed "CREATE INDEX ON ..."; named here so IF NOT EXISTS works.
CREATE INDEX IF NOT EXISTS call_events_status_received_at_idx
    ON hotline.call_events (status, received_at);

-- One row per Claude attempt (attempt 1, and attempt 2 on schema-invalid retry).
CREATE TABLE IF NOT EXISTS hotline.extraction_runs (
    id                bigserial PRIMARY KEY,
    conversation_id   text NOT NULL REFERENCES hotline.call_events,
    attempt           smallint NOT NULL,
    prompt_version    text NOT NULL,
    model             text NOT NULL,
    output            jsonb,
    is_valid          boolean NOT NULL,
    validation_errors jsonb,
    latency_ms        int NOT NULL,
    input_tokens      int,
    output_tokens     int,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- One routing decision per call. For AUTO_WRITE, decided_at is set when the
-- Salesforce write succeeds (so hang-up-to-write latency is end to end).
CREATE TABLE IF NOT EXISTS hotline.decisions (
    conversation_id  text PRIMARY KEY REFERENCES hotline.call_events,
    route            text NOT NULL CHECK (route IN
                         ('LOGGED_ONLY','AUTO_WRITE','APPROVAL')),
    intent_score     smallint,
    approval_outcome text CHECK (approval_outcome IN
                         ('APPROVED','REJECTED','EXPIRED')),
    approver         text,
    decided_at       timestamptz,
    sf_lead_id       text,
    sf_task_id       text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
