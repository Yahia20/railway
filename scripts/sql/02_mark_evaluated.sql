-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Mark evaluated'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $('Claim work').item.json.uniqueid, $('Claim work').item.json.claim_token ] }}
-- fence-exempt: single-statement UPDATE. The fence IS the statement's own
-- WHERE clause, so the row is locked and the predicate re-evaluated against
-- the latest committed version by the UPDATE itself (EvalPlanQual). There is
-- no read-then-write window to widen, and adding FOR UPDATE would only take
-- the same lock twice.
-- Terminal success. The lease is released and last_error cleared, so a row that
-- previously failed does not keep a stale reason attached to a good result.
UPDATE call_ingest_jobs
SET status          = 'evaluated',
    last_error      = NULL,
    next_attempt_at = now(),
    claim_token     = NULL,
    claimed_at      = NULL,
    claim_until     = NULL,
    updated_at      = now()
WHERE uniqueid = $1
  AND claim_token = $2::uuid
  AND status = 'evaluating'
RETURNING uniqueid, status;
