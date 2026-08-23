-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Recover expired leases'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ 45, 5 ] }}
-- lease-exempt: this IS the lease breaker. Matching on claim_token would
-- defeat its only purpose, which is to reclaim rows from workers that are
-- gone. It is safe because it only touches rows whose claim_until has
-- already passed, and it clears the token as it goes.
-- Give back the work of executions that died holding a lease.
--
-- 'transcribing' and 'evaluating' mean "a worker holds this row right now".
-- If the worker crashed, was cancelled, or hung past claim_until, nothing else
-- would ever pick the row up again: it is not 'discovered', not failed, and
-- not terminal, so no claim predicate matches it. That is a permanent loss of
-- exactly the kind 008 exists to prevent, and it is invisible -- the row looks
-- busy forever.
--
-- Recovery puts the row back in the stage-appropriate failed state so the
-- normal retry budget applies, and only dead-letters it once that budget is
-- spent. The cooldown is the same 45 minutes the failure paths use: a run that
-- died because the ASR Space was throttling must not be re-claimed instantly.
--
-- The attempt was already counted at claim time, so recovery does not count it
-- again -- that is the whole point of counting at claim: a worker that dies
-- without writing anything still spent an attempt, and a poison row cannot be
-- claimed forever.
--
-- $1 = cooldown minutes, $2 = max attempts per stage.
UPDATE call_ingest_jobs j
SET status = CASE
      WHEN j.status = 'transcribing' AND j.asr_attempts   >= $2 THEN 'dead_letter'
      WHEN j.status = 'evaluating'   AND j.judge_attempts >= $2 THEN 'dead_letter'
      WHEN j.status = 'transcribing'                            THEN 'asr_failed'
      ELSE                                                           'judge_failed'
    END,
    last_error = left('lease_expired: held as ' || j.status
                      || ' by token ' || coalesce(j.claim_token::text, 'none')
                      || ', lease ended ' || to_char(j.claim_until, 'YYYY-MM-DD HH24:MI:SSOF')
                      || ' (asr_attempts=' || j.asr_attempts
                      || ', judge_attempts=' || j.judge_attempts || ')', 500),
    next_attempt_at = now() + ($1 * interval '1 minute'),
    retries       = j.retries + 1,
    claim_token   = NULL,
    claimed_at    = NULL,
    claim_until   = NULL,
    updated_at    = now()
WHERE j.status IN ('transcribing', 'evaluating')
  AND j.claim_until IS NOT NULL
  AND j.claim_until < now()
RETURNING j.uniqueid, j.status, j.asr_attempts, j.judge_attempts, j.last_error;
