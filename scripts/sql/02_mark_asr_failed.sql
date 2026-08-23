-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Mark ASR failed'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $('Claim work').item.json.uniqueid, $('Claim work').item.json.claim_token, 5, JSON.stringify({ error: $json.error || 'empty or low-confidence transcript', confidence: ($json.confidence !== undefined ? $json.confidence : ($json.asr_confidence !== undefined ? $json.asr_confidence : 0)), text_len: ($json.full_text || $json.dialogue || '').length }), 45 ] }}
-- fence-exempt: single-statement UPDATE. The fence IS the statement's own
-- WHERE clause, so the row is locked and the predicate re-evaluated against
-- the latest committed version by the UPDATE itself (EvalPlanQual). There is
-- no read-then-write window to widen, and adding FOR UPDATE would only take
-- the same lock twice.
-- Retryable ASR failure: nothing usable came back, or what came back is below
-- the confidence floor. Release the lease, spend the cool-down, and let the
-- normal claim pick it up again -- unless the per-stage attempt budget is gone,
-- in which case it is a dead letter and a human problem.
--
-- The 45-minute cool-down is not politeness: the free ASR Space throttles under
-- rapid repeat batches, and without it one temporary throttle burns the retry
-- budget of the entire backlog inside an hour while fresh work starves.
--
-- status IN ('transcribing','evaluating') because this node is reachable from
-- both stages: a job claimed for evaluation whose stored transcript turns out
-- to be unusable needs re-transcription, not another judge attempt. Its claim
-- spent a judge attempt rather than an ASR one, which slightly over-counts the
-- judge budget for that row -- recorded here rather than papered over.
--
-- $1 = uniqueid, $2 = claim token, $3 = max attempts, $4 = error json,
-- $5 = cooldown minutes.
UPDATE call_ingest_jobs
SET status = CASE WHEN asr_attempts >= $3 THEN 'dead_letter' ELSE 'asr_failed' END,
    retries         = retries + 1,
    last_error      = left($4, 500),
    next_attempt_at = now() + ($5 * interval '1 minute'),
    claim_token     = NULL,
    claimed_at      = NULL,
    claim_until     = NULL,
    updated_at      = now()
WHERE uniqueid = $1
  AND claim_token = $2::uuid
  AND status IN ('transcribing', 'evaluating')
RETURNING uniqueid, status;
