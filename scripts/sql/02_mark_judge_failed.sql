-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Mark judge failed'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $('Claim work').item.json.uniqueid, $('Claim work').item.json.claim_token, 5, JSON.stringify((() => { const d = $('Two AI passes').item.json; if (!d || d.error) return { error: (d && d.error) || 'no response' }; if (!d.pass1) return { error: 'pass1 missing' }; const p2 = d.pass2; if (!p2) return { error: 'pass2 missing' }; return { error: 'contract_failed', contract_violations: p2.contract_violations, evidence_rejected: p2.evidence_rejected }; })()), 45 ] }}
-- fence-exempt: single-statement UPDATE. The fence IS the statement's own
-- WHERE clause, so the row is locked and the predicate re-evaluated against
-- the latest committed version by the UPDATE itself (EvalPlanQual). There is
-- no read-then-write window to widen, and adding FOR UPDATE would only take
-- the same lock twice.
-- Retryable judge failure: a 422 (the response was not usable JSON at all), a
-- transport error, or a response that broke the rubric contract after the
-- re-ask. No agent_evaluations row is written -- a score the scoring engine
-- refused to compute must not appear as a number.
--
-- judge_attempts, not the shared retries counter: an ASR problem and a judge
-- problem now have separate budgets, so a bad prompt version can no longer
-- dead-letter calls whose audio was fine.
--
-- $1 = uniqueid, $2 = claim token, $3 = max attempts, $4 = error json,
-- $5 = cooldown minutes.
UPDATE call_ingest_jobs
SET status = CASE WHEN judge_attempts >= $3 THEN 'dead_letter' ELSE 'judge_failed' END,
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
