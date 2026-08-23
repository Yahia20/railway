-- 008: durable per-call pipeline state.
--
-- The calls workflow used to gate on "is this interaction new?" plus a
-- success-watermark in job_runs. Both produced permanent losses in the first
-- live week: a crash between store and evaluate stranded 14 calls forever
-- (the is_new gate then skipped them), and the watermark missed the whole
-- 9-8 day folder because its files were uploaded before the watermark was
-- first set. This table makes each recording a unit of recoverable work:
-- every stage moves a status forward, every failure is retryable with a
-- bounded count, and discovery re-registers idempotently from a wide listing
-- window instead of a cursor.
--
-- Statuses:
--   discovered   listed on Drive, not yet transcribed
--   transcribed  interaction + transcript stored, not yet evaluated
--   evaluated    terminal success
--   asr_failed   transcript empty or low-confidence; retried next run
--   judge_failed evaluation returned 422; retried next run
--   dead_letter  3 retries burned; needs a human (or a fix) to re-open

CREATE TABLE IF NOT EXISTS call_ingest_jobs (
    uniqueid       text PRIMARY KEY,           -- Asterisk uniqueid: the canonical call key.
                                               -- Drive file ids are storage, not identity:
                                               -- the same call arrives as "name (1).wav" copies.
    filename       text  NOT NULL DEFAULT '',
    audio_uri      text  NOT NULL DEFAULT '',
    meta           jsonb NOT NULL DEFAULT '{}'::jsonb,   -- /calls/list row, verbatim
    status         text  NOT NULL DEFAULT 'discovered'
        CHECK (status IN ('discovered','transcribed','evaluated',
                          'asr_failed','judge_failed','dead_letter')),
    interaction_id uuid REFERENCES interactions(interaction_id),
    retries        int   NOT NULL DEFAULT 0,
    last_error     text,
    discovered_at  timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_call_ingest_jobs_status
    ON call_ingest_jobs (status);

-- Seed from what already ran so the first wide listing does not redo history:
-- evaluated calls are terminal; stored-but-never-evaluated calls (the stranded
-- class this table exists to kill) re-enter as 'discovered' with empty meta,
-- which the register step refills from the next listing before they are claimed.
INSERT INTO call_ingest_jobs (uniqueid, audio_uri, status, interaction_id)
SELECT i.external_id,
       coalesce(t.audio_uri, ''),
       CASE WHEN e.interaction_id IS NULL THEN 'discovered' ELSE 'evaluated' END,
       i.interaction_id
FROM interactions i
LEFT JOIN transcripts t       ON t.interaction_id = i.interaction_id
LEFT JOIN agent_evaluations e ON e.interaction_id = i.interaction_id
WHERE i.external_source = 'asterisk_drive'
ON CONFLICT (uniqueid) DO NOTHING;
