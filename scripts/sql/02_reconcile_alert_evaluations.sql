-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Reconcile alert evaluations'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ 200 ] }}
-- lease-exempt: reconciliation operates on TERMINAL, UNLEASED job rows -- rows
-- that by definition nobody holds a lease on and nobody will claim again. There
-- is no token to carry. It takes its own rows with FOR UPDATE SKIP LOCKED
-- inside reconcile_alert_evaluations(), so it never waits for, and never
-- overwrites, work that a live execution is holding.
-- The other half of "alert-queue persistence is not best effort".
--
-- 'Evaluate alert rules' stamps call_ingest_jobs.alerts_evaluated_at in the
-- same statement as the evaluation. Anything that did not get stamped -- a
-- connection that dropped, a deadlock, a bug in one rule, an execution that was
-- cancelled between the terminal update and the alert node -- is a terminal job
-- with alerts_evaluated_at IS NULL, and this node is what finishes it. Running
-- it repeatedly is free: evaluate_alert_rules() deduplicates on
-- ON CONFLICT (rule_code, rule_version, interaction_id, occurrence_hash).
--
-- It hangs off the trigger, NOT off the claim chain: it must run on a day with
-- no new recordings and no claimable work, which is exactly the kind of quiet
-- day the old chained-claim bug hid behind.
--
-- Per-row subtransactions inside the function mean one poison interaction
-- records its own error in alerts_error and is skipped, rather than aborting
-- the batch forever. See db/migrations/013_alert_rules.sql.
--
-- $1 = how many jobs to reconcile per sweep.
SELECT job_uniqueid,
       job_interaction_id,
       occurrences_recorded,
       error_text
FROM reconcile_alert_evaluations($1)
ORDER BY error_text NULLS LAST, job_uniqueid;
