-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Dead-letter judge budget'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $('Claim work').item.json.uniqueid, $('Claim work').item.json.claim_token, 5 ] }}
-- fence-exempt: single-statement UPDATE. The fence IS the statement's own
-- WHERE clause, so the row is locked and the predicate re-evaluated against
-- the latest committed version by the UPDATE itself (EvalPlanQual). There is
-- no read-then-write window to widen, and adding FOR UPDATE would only take
-- the same lock twice.
-- The judge budget is gone and no judge will ever be called for this row: say
-- so, once, instead of parking it for three hours.
--
-- WHAT THIS REPLACES. 'Begin judge attempt' returns zero rows when the row is
-- still 'transcribing' and judge_attempts is already at the cap -- reachable by
-- the abnormal re-transcription path (a row that hit the cap, then failed the
-- ASR quality gate, then was re-transcribed). The false output of 'Judge
-- attempt started?' used to be a bare leaf: the chain stopped, the lease lapsed
-- three hours later, the sweep re-queued the row as ASR work, and it burned ASR
-- attempts one at a time until THAT budget was gone too -- re-transcribing
-- audio for a judge that could never run. Terminating, but expensive and
-- confusing in the dead-letter list.
--
-- WHY IT READS NOTHING FROM THE JUDGE BRANCH. The Two-AI-passes node has not
-- run on this output, so reaching into it would be exactly the cross-branch
-- reference the validator exists to catch -- and it is why 'Mark judge failed'
-- could not be reused here. Everything below comes from the claim, which is
-- upstream of both outputs.
--
-- THE OTHER FALSE CASE IS A STALE TOKEN, and it must stay a no-op: if this
-- execution lost the lease, somebody else owns the row and dead-lettering it
-- would be the exact overwrite this PR removes. Both halves of the fence -- the
-- token AND `judge_attempts >= $3` -- have to hold, so a stale token produces
-- UPDATE 0 and the node is a leaf, which is the end of it.
--
-- $1 = uniqueid, $2 = claim token, $3 = max attempts per stage.
UPDATE call_ingest_jobs
SET status          = 'dead_letter',
    last_error      = 'judge_budget_exhausted_before_handoff',
    retries         = retries + 1,
    next_attempt_at = now(),
    claim_token     = NULL,
    claimed_at      = NULL,
    claim_until     = NULL,
    updated_at      = now()
WHERE uniqueid       = $1
  AND claim_token    = $2::uuid
  AND status         = 'transcribing'
  AND judge_attempts >= $3
RETURNING uniqueid, status, asr_attempts, judge_attempts, last_error;
