-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Store pass1'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $('Prepare evaluation input').item.json.interaction_id, JSON.stringify($('Two AI passes').item.json), $('Claim work').item.json.uniqueid, $('Claim work').item.json.claim_token ] }}
-- Pass 1 is stored on its OWN merit: a good customer extraction naming a hot
-- lead with a verified quote must survive a pass-2 failure, because it is the
-- half that drives revenue.
--
-- pass1_validation is written by the worker INTO the payload
-- (judge.py: payload["pass1_validation"] = validation) and also lifted out to
-- pass1.pass1_validation. The || merge below puts it back inside the stored
-- payload whichever side a future worker writes it to, because
-- evaluate_alert_rules() reads it from raw_response.
--
-- FENCED. `r` selects FROM lease, so an execution that lost its lease writes
-- nothing and returns nothing, and 'Pass 1 stored?' stops the chain.
--
-- ON CONFLICT UPDATES EVERY VALUE DERIVED FROM THIS RESPONSE. A re-evaluation
-- used to refresh intent/summary/confidence/raw_response and leave
-- prompt_version, model, language and uncertain_fields describing the run that
-- did NOT produce the stored payload -- the exact shape of provenance bug that
-- makes a prompt A/B unreadable six weeks later.
--
-- ATOMIC AGAINST RECOVERY (round-2 blocker). The fence used to be an UNLOCKED
-- `SELECT 1 FROM call_ingest_jobs WHERE ... claim_token = $token`. That reads a
-- snapshot and then lets go: the recovery sweep could reclaim the row, and a
-- newer worker re-claim it, in the window between that read and the upsert
-- below, and the stale write still committed. The lease CTE now takes a real
-- row lock with FOR UPDATE, and requires the lease to still be LIVE
-- (claim_until > now()). Two orderings, both safe:
--
--   * recovery gets the lock first -> this statement BLOCKS on it, and when it
--     is released Postgres re-evaluates the qualification against the NEW row
--     version (EvalPlanQual). The token is gone, so the CTE yields no row, the
--     dependent write is empty, and the node returns zero rows.
--   * this statement gets the lock first -> recovery BLOCKS until we commit,
--     then re-checks its own `claim_until < now()` predicate against what we
--     wrote.
--
-- Postgres rule that dictates the SHAPE: a data-modifying CTE cannot see the
-- effects of its sibling CTEs, and sub-statements all run on one snapshot. So
-- the lock has to be what the write READS FROM, not a check standing beside it
-- -- every dependent CTE below selects (transitively) from `lease`, and the
-- final statement joins it. AS MATERIALIZED pins that: the CTE is evaluated
-- once, first, and cannot be inlined into the readers.
--
-- Locking order for the whole workflow is documented in docs/PR1A-leases.md
-- section 2 ("Locking order"). Short version: every statement takes
-- call_ingest_jobs FIRST and, apart from the claim and the sweep, takes exactly
-- ONE job row, by primary key; the claim uses SKIP LOCKED so it never waits.
WITH lease AS MATERIALIZED (
  SELECT j.uniqueid
  FROM call_ingest_jobs j
  WHERE j.uniqueid    = $3
    AND j.claim_token = $4::uuid
    AND j.status IN ('evaluating')
    AND j.claim_until > now()
  FOR UPDATE
),
r AS (SELECT $2::jsonb AS d FROM lease),
p AS (
  SELECT coalesce(d->'pass1'->'payload', '{}'::jsonb)
         || jsonb_build_object('pass1_validation',
              coalesce(d->'pass1'->'payload'->'pass1_validation',
                       d->'pass1'->'pass1_validation',
                       '{}'::jsonb)) AS payload,
         d
  FROM r
)
INSERT INTO interaction_analysis (
  interaction_id, schema_version, prompt_version, model, input_type,
  language, intent, summary_ar, confidence, uncertain_fields, raw_response
)
SELECT
  $1::uuid,
  '1.0',
  p.d->'pass1'->>'prompt_version',
  p.d->'pass1'->>'model',
  'call_transcript'::input_type,
  left(p.payload->>'language', 2),
  p.payload->>'intent',
  p.payload->>'summary_ar',
  (p.payload->>'confidence')::numeric,
  coalesce(p.payload->'uncertain_fields', '[]'::jsonb),
  p.payload
FROM p
ON CONFLICT (interaction_id) DO UPDATE SET
  schema_version   = EXCLUDED.schema_version,
  prompt_version   = EXCLUDED.prompt_version,
  model            = EXCLUDED.model,
  input_type       = EXCLUDED.input_type,
  language         = EXCLUDED.language,
  intent           = EXCLUDED.intent,
  summary_ar       = EXCLUDED.summary_ar,
  confidence       = EXCLUDED.confidence,
  uncertain_fields = EXCLUDED.uncertain_fields,
  raw_response     = EXCLUDED.raw_response,
  updated_at       = now()
RETURNING interaction_id;
