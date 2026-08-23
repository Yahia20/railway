-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Begin judge attempt'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $('Claim work').item.json.uniqueid, $('Claim work').item.json.claim_token, 3600, 5 ] }}
-- fence-exempt: single-statement UPDATE. The fence IS the statement's own
-- WHERE clause, so the row is locked and the predicate re-evaluated against
-- the latest committed version by the UPDATE itself (EvalPlanQual). There is
-- no read-then-write window to widen, and adding FOR UPDATE would only take
-- the same lock twice.
-- The transcribe->evaluate handoff, moved to AFTER the quality gates.
--
-- WHAT THIS FIXES. The old 'Link job transcribed' incremented judge_attempts at
-- the moment the transcript was stored -- before 'ASR quality red?' and
-- 'Confidence usable?' had looked at it. Every red or low-confidence transcript
-- therefore spent one of the five judge attempts without a judge request ever
-- being made, so five bad recordings' worth of ASR quality could dead-letter a
-- call the judge had never seen.
--
-- ONE STATEMENT SERVES BOTH PATHS, which is why it does not simply say
-- status = 'transcribing':
--   * transcribe path -- the row is still 'transcribing'. Move it to
--     'evaluating' and spend a judge attempt, but only if the budget allows.
--   * evaluate path   -- 'Claim work' already set 'evaluating' and already
--     spent the attempt (and already enforced the cap). Do not double-count;
--     just renew the lease for the judge phase.
-- Returning a row in BOTH cases is deliberate: the caller's gate treats zero
-- rows as "the lease is gone", and a silent no-op would be indistinguishable.
--
-- The cap is re-checked here because an abnormal re-transcription path can
-- reach a row whose judge_attempts is already at the ceiling. Zero rows then
-- stops the chain and the lease sweep re-queues the row; see
-- docs/PR1A-leases.md section 10 for why that is the intended, terminating
-- behaviour rather than an immediate dead_letter.
UPDATE call_ingest_jobs
SET status         = 'evaluating',
    judge_attempts = judge_attempts
                     + CASE WHEN status = 'transcribing' THEN 1 ELSE 0 END,
    claim_until    = now() + ($3 * interval '1 second'),
    last_error     = NULL,
    updated_at     = now()
WHERE uniqueid    = $1
  AND claim_token = $2::uuid
  AND status IN ('transcribing', 'evaluating')
  AND (status = 'evaluating' OR judge_attempts < $4)
RETURNING uniqueid, interaction_id, status, judge_attempts;
