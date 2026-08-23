-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Claim work'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ 6, 10800, 5 ] }}
-- lease-exempt: this is where the lease is TAKEN. It selects only rows with
-- claim_until IS NULL and stamps a fresh token under FOR UPDATE SKIP LOCKED.
-- Atomically take ownership of a bounded batch of work.
--
-- The old version only stamped updated_at. Two overlapping executions -- the
-- schedule fires every 15 minutes and a batch of 25 recordings routinely runs
-- longer than that -- therefore selected the SAME rows and both transcribed and
-- both judged them: double Cohere spend, double DeepSeek spend, and two
-- writers racing for one transcripts row.
--
-- What makes this claim safe:
--   FOR UPDATE SKIP LOCKED  a concurrent claim walks past locked candidates
--                           instead of blocking on them or taking them.
--   status -> transcribing / evaluating
--                           the row is visibly in flight, so it matches no
--                           claim predicate until it is released or its lease
--                           expires and the recovery sweep reopens it.
--   claim_token             stamped here and required by every terminal write,
--                           so an execution that lost its lease cannot
--                           overwrite whoever picked the work up afterwards.
--   per-stage attempts      incremented for the stage actually being entered,
--                           so a judge bug can no longer burn the ASR retry
--                           budget of the whole backlog.
--
-- previous_status is returned alongside work_stage because the failure paths
-- need to know which stage they are failing, and this node's output is the
-- authoritative copy of that for the rest of the run.
--
-- $1 = batch size, $2 = lease seconds, $3 = max attempts per stage.
WITH candidates AS (
  SELECT j.uniqueid,
         j.status AS previous_status
  FROM call_ingest_jobs j
  WHERE j.meta <> '{}'::jsonb
    AND j.claim_until IS NULL
    AND j.next_attempt_at <= now()
    AND (
          (j.status IN ('discovered', 'asr_failed')    AND j.asr_attempts   < $3)
       OR (j.status IN ('transcribed', 'judge_failed') AND j.judge_attempts < $3)
    )
  ORDER BY j.next_attempt_at, j.discovered_at, j.uniqueid
  FOR UPDATE SKIP LOCKED
  LIMIT $1
)
UPDATE call_ingest_jobs j
SET status = CASE WHEN c.previous_status IN ('discovered', 'asr_failed')
                  THEN 'transcribing' ELSE 'evaluating' END,
    asr_attempts   = j.asr_attempts
                     + CASE WHEN c.previous_status IN ('discovered', 'asr_failed')
                            THEN 1 ELSE 0 END,
    judge_attempts = j.judge_attempts
                     + CASE WHEN c.previous_status IN ('transcribed', 'judge_failed')
                            THEN 1 ELSE 0 END,
    claim_token = gen_random_uuid(),
    claimed_at  = now(),
    claim_until = now() + ($2 * interval '1 second'),
    updated_at  = now()
FROM candidates c
WHERE j.uniqueid = c.uniqueid
RETURNING j.uniqueid,
          j.filename,
          j.audio_uri,
          j.meta,
          j.interaction_id,
          j.claim_token,
          j.claim_until,
          j.asr_attempts,
          j.judge_attempts,
          c.previous_status,
          CASE WHEN c.previous_status IN ('discovered', 'asr_failed')
               THEN 'transcribe' ELSE 'evaluate' END AS work_stage;
