-- 025 — retention runs again: purge_raw_content() without the calls lane.
--
-- WHAT BROKE. 023 dropped `transcripts` and did not redefine
-- purge_raw_content() (017), whose second block reads and updates that table.
-- PL/pgSQL resolves table names when a statement first runs, so the function
-- still compiled and the failure only appeared at 03:20 the next night:
--
--   relation "transcripts" does not exist
--
-- every night from 2026-09-16. The whole call is one transaction, so the chat
-- deletes that run BEFORE the transcripts block were rolled back with it:
-- nothing has been purged since 023. Workflow 04's "Purge raw content" node has
-- onError continueRegularOutput, so the run carried on to the health check,
-- which failed on the same table and is the only error anyone saw.
--
-- No chat was overdue yet (the oldest stored thread is 2026-08-23, so the
-- 90-day window first bites on 2026-11-21). This must be applied before then.
--
-- THE SIGNATURE IS UNCHANGED, including p_call_days, which no longer does
-- anything. Workflow 04 calls purge_raw_content(90, 365, false); changing the
-- arity would turn this fix into a second outage until the workflow followed.

BEGIN;

SET lock_timeout = '5s';

CREATE OR REPLACE FUNCTION purge_raw_content(
  p_chat_days int DEFAULT 90,
  p_call_days int DEFAULT 365,   -- kept for callers; there are no calls any more
  p_dry_run   boolean DEFAULT true
) RETURNS TABLE (what text, rows_affected bigint)
LANGUAGE plpgsql AS $fn$
DECLARE
  chat_cut timestamptz := now() - make_interval(days => p_chat_days);
  n bigint;
BEGIN
  -- Chat message bodies.
  IF p_dry_run THEN
    SELECT count(*) INTO n FROM chat_messages m
      JOIN interactions i USING (interaction_id)
     WHERE i.started_at < chat_cut AND i.content_purged_at IS NULL;
  ELSE
    WITH doomed AS (
      DELETE FROM chat_messages m
       USING interactions i
       WHERE i.interaction_id = m.interaction_id
         AND i.started_at < chat_cut
         AND i.content_purged_at IS NULL
      RETURNING m.message_id
    ) SELECT count(*) INTO n FROM doomed;
  END IF;
  what := 'chat_messages deleted'; rows_affected := n; RETURN NEXT;

  -- Raw webhook payloads: a byte-for-byte copy of what is already parsed.
  IF p_dry_run THEN
    SELECT count(*) INTO n FROM raw_events WHERE received_at < chat_cut;
  ELSE
    WITH doomed AS (
      DELETE FROM raw_events WHERE received_at < chat_cut RETURNING raw_id
    ) SELECT count(*) INTO n FROM doomed;
  END IF;
  what := 'raw_events deleted'; rows_affected := n; RETURN NEXT;

  -- The model's own transcript of its reasoning, which quotes the customer.
  -- Every field parsed out of it is already in its own column.
  IF p_dry_run THEN
    SELECT count(*) INTO n FROM interaction_analysis a
      JOIN interactions i USING (interaction_id)
     WHERE i.started_at < chat_cut AND a.raw_response IS NOT NULL;
  ELSE
    WITH doomed AS (
      UPDATE interaction_analysis a SET raw_response = NULL
        FROM interactions i
       WHERE i.interaction_id = a.interaction_id
         AND i.started_at < chat_cut
         AND a.raw_response IS NOT NULL
      RETURNING a.analysis_id
    ) SELECT count(*) INTO n FROM doomed;
  END IF;
  what := 'pass1 raw_response blanked'; rows_affected := n; RETURN NEXT;

  IF p_dry_run THEN
    SELECT count(*) INTO n FROM agent_evaluations e
      JOIN interactions i USING (interaction_id)
     WHERE i.started_at < chat_cut AND e.raw_response IS NOT NULL;
  ELSE
    WITH doomed AS (
      UPDATE agent_evaluations e SET raw_response = NULL
        FROM interactions i
       WHERE i.interaction_id = e.interaction_id
         AND i.started_at < chat_cut
         AND e.raw_response IS NOT NULL
      RETURNING e.evaluation_id
    ) SELECT count(*) INTO n FROM doomed;
  END IF;
  what := 'pass2 raw_response blanked'; rows_affected := n; RETURN NEXT;

  -- Stamp last. A thread is only marked purged once its words are actually
  -- gone, so a crash halfway through leaves it eligible for the next run
  -- instead of marked done with content still present.
  IF NOT p_dry_run THEN
    WITH stamped AS (
      UPDATE interactions SET content_purged_at = now()
       WHERE started_at < chat_cut AND content_purged_at IS NULL
      RETURNING interaction_id
    ) SELECT count(*) INTO n FROM stamped;
    what := 'interactions stamped'; rows_affected := n; RETURN NEXT;
  END IF;
END
$fn$;

COMMENT ON FUNCTION purge_raw_content(int, int, boolean) IS
  'Deletes raw customer words older than p_chat_days and stamps '
  'interactions.content_purged_at. Analysis, scores, requests and source ids '
  'are never touched. p_call_days is accepted and ignored since 023 removed '
  'calls. Dry run by default — pass p_dry_run => false to apply.';

COMMIT;
