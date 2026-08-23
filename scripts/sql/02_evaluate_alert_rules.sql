-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Evaluate alert rules'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $('Prepare evaluation input').item.json.interaction_id, $('Claim work').item.json.uniqueid, $json.status ] }}
-- The follow-up QUEUE, not a notification. Recordings reach us roughly a day
-- after the call, so there is nothing real-time to alert on and nothing here
-- POSTs anywhere: evaluate_alert_rules() records what fired into
-- alert_occurrences, v_alert_queue is the working list and v_alert_digest_daily
-- is the daily read. Delivery is somebody reading that queue.
--
-- NOT BEST EFFORT ANY MORE (round-2 blocker). This node runs AFTER the job is
-- terminal, and a terminal job is never claimed again and never recovered by
-- the lease sweep -- so a transient failure here used to lose that call's
-- occurrences permanently, silently, with the execution reported green because
-- the node was a leaf with onError: continueRegularOutput. Three changes:
--
--   1. The stamp. `alerts_evaluated_at` is set in the SAME statement as the
--      evaluation, so it can never claim work that did not happen: if the
--      function raises, the statement aborts and the column stays NULL.
--   2. The node no longer swallows errors. A failure fails the execution, which
--      is how you find out about it.
--   3. Whatever is still missing is reconciled. 'Reconcile alert evaluations'
--      re-runs the rules for every terminal job with alerts_evaluated_at IS
--      NULL, on every sweep. Free, because evaluate_alert_rules() deduplicates
--      on ON CONFLICT (rule, version, interaction, fact hash).
--
-- Why not fold this into 'Mark evaluated' instead -- the atomic alternative --
-- is argued in db/migrations/013_alert_rules.sql, section "ALERT EVALUATION IS
-- DURABLE WORK". Short version: it would make one broken triage rule roll back
-- every terminalization and re-spend the judge budget on every cycle.
--
-- FENCED, three ways over:
--   1. Structurally -- reachable only from 'Job finalised?', which requires
--      'Mark evaluated' or 'Mark judge failed' to have actually returned a row.
--   2. In SQL, with a LOCK -- `job` takes the row FOR UPDATE, so a concurrent
--      re-claim either loses the race and waits, or wins it and this CTE sees
--      the new version and yields nothing. The terminal update has already
--      cleared claim_token, so the predicate is "still in the terminal state
--      this execution just wrote ($3), and nobody has re-claimed it".
--   3. Every dependent CTE reads FROM `job`, because a data-modifying CTE
--      cannot see the effects of a sibling. `fired` must stay referenced by the
--      final SELECT: unlike a data-modifying CTE, a CTE that merely CALLS a
--      writing function is not guaranteed to run if nothing reads it.
--
-- The LEFT JOIN to `fired` is deliberate: a call that trips no rule is the
-- normal case and must still return its stamp row, so that zero rows from this
-- node means one thing only -- the fence failed.
--
-- delivery_status 'pending' means "in the follow-up queue"; the workflow never
-- changes it. A human (or a nightly digest consumer) moves it to
-- 'acknowledged'. 'suppressed' is what a rule with is_alert = false records.
WITH job AS MATERIALIZED (
  SELECT j.uniqueid, j.interaction_id
  FROM call_ingest_jobs j
  WHERE j.uniqueid       = $2
    AND j.interaction_id = $1::uuid
    AND j.claim_token IS NULL
    AND j.status         = $3
  FOR UPDATE
),
fired AS (
  SELECT o.*
  FROM job
  CROSS JOIN LATERAL evaluate_alert_rules(job.interaction_id) o
),
stamped AS (
  UPDATE call_ingest_jobs j
  SET alerts_evaluated_at = now(),
      alerts_error        = NULL,
      updated_at          = now()
  FROM job
  WHERE j.uniqueid = job.uniqueid
  RETURNING j.uniqueid, j.interaction_id, j.status, j.alerts_evaluated_at
)
SELECT s.uniqueid,
       s.status,
       s.alerts_evaluated_at,
       o.occurrence_id,
       o.rule_code,
       o.rule_version,
       o.delivery_status,
       o.fact_snapshot,
       o.created_at,
       i.interaction_id,
       i.started_at,
       i.customer_phone_e164,
       coalesce(a.full_name, 'unassigned') AS agent_name
FROM stamped s
LEFT JOIN fired o        ON o.interaction_id = s.interaction_id
                        AND o.delivery_status = 'pending'
LEFT JOIN interactions i ON i.interaction_id = s.interaction_id
LEFT JOIN agents a       ON a.agent_id = i.agent_id
ORDER BY o.created_at, o.rule_code;
