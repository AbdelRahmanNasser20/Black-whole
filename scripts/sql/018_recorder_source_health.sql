-- 018_recorder_source_health.sql — per-source circuit breaker for the recorder.
--
-- STATUS: APPLIED to prod 2026-10-02 (main session, Supabase). Re-apply
-- elsewhere with `.venv/bin/python scripts/apply_sql.py scripts/sql/018_recorder_source_health.sql`.
-- Where it is absent the recorder keeps the breaker in memory for one run and
-- prints a RECORDER NOTE; nothing fails.
--
-- One row per source, written once per `recorder run` (recorder/health.py,
-- recorder/store.py::save_source_health). ~8 rows total; no growth.
--   state: closed → open (after RECORDER_BREAKER_FAILURES failed attempts)
--          → half_open (one probe at next_attempt_at) → closed on success.

CREATE TABLE IF NOT EXISTS recorder_source_health (
    source               text PRIMARY KEY,
    state                text NOT NULL DEFAULT 'closed'
                         CHECK (state IN ('closed', 'open', 'half_open')),
    consecutive_failures integer NOT NULL DEFAULT 0,
    last_attempt_at      timestamptz,
    last_success_at      timestamptz,
    next_attempt_at      timestamptz,
    last_error           text,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- RLS: same posture as every other table today (disabled; server boundary is
-- the security boundary). Covered by the workspace-wide RLS rollout task.
