-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Store evaluation'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $('Prepare evaluation input').item.json.interaction_id, JSON.stringify(Object.assign({}, $('Two AI passes').item.json, { asr_confidence: $('Prepare evaluation input').item.json.asr_confidence })), $('Claim work').item.json.uniqueid, $('Claim work').item.json.claim_token ] }}
-- FENCED, like every other durable write here: `r` selects FROM lease.
--
-- STATUS IS FIRST-CLASS (014). contract_status / gradeable /
-- ungradeable_modules / evidence_rejected / model_fingerprint are written
-- as COLUMNS, not left buried in raw_response. Every consumer needs to know
-- whether this row carries a score before it averages anything, and
-- reaching into a jsonb blob to find that out is how the old scorecard
-- ended up counting ungradeable rows in its denominator and skipping them
-- in its numerator.
--
-- WHICH STATUSES REACH THIS NODE. ok, ungradeable and unscoreable -- all
-- three are stored. Ungradeable and unscoreable are TERMINAL DATA QUALITY,
-- not judge faults: retrying them re-asks a model that already answered and
-- can only manufacture a score. contract_failed never gets here; 'Pass 2
-- usable?' routes it to 'Mark judge failed', which is retryable.
--
-- MISSING KEYS ARE THE OLD WORKER, NOT A FAILURE. An older /evaluate that
-- does not send these fields must keep working, so absent reads as
-- ok / true / [] / [] / null -- the same defaults 014 gives the columns.
--
-- model_fingerprint IS THE LAST FINGERPRINT SEEN. An evaluation is one or two
-- API calls (the contract re-ask is the second), and the worker records a
-- disagreement between them as usage.system_fingerprint_all. The column takes
-- usage.system_fingerprint, which is the last value; the *_all list is not
-- stored anywhere, because raw_response holds pass2.payload and not
-- pass2.usage. A score whose two halves came from different backends is
-- therefore grouped under one of them. See docs/PR2-db-status.md section 5.
--
-- gradeable IS AND-ed WITH "a score arrived". A response claiming
-- gradeable = true with a null final_score is self-contradictory, and
-- storing it that way would put a row in the 'ok, graded, but no number'
-- state that no view has a bucket for. This keeps the invariant the 014
-- backfill established: gradeable implies final_score IS NOT NULL.
--
-- ON CONFLICT REFRESHES EVERY COLUMN DERIVED FROM THE RESPONSE. The first
-- version updated final_score, performance_level, weight_applied and
-- raw_response only, which meant a re-judged call kept the OLD module scores,
-- breakdown, evidence, stage, behaviour flags, recommendations and
-- prompt/rubric/model beside the NEW raw_response. Every scorecard, every
-- coaching export and every prompt comparison then read a row that was half one
-- run and half another, with nothing to show it.
--
-- agent_id and source_quality are refreshed too: agent_id because the agents
-- table can gain the extension after the first evaluation, source_quality
-- because a re-transcription changes the confidence the score rests on.
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
r AS (SELECT $2::jsonb AS d FROM lease)
INSERT INTO agent_evaluations (
  interaction_id, agent_id, schema_version, prompt_version, rubric_version, model,
  input_type, source_quality, final_score, performance_level, weight_applied,
  m1_reception, m2_offer, m3_objections, m4_followup, m5_closing,
  breakdown, evidence, stage_reached, behavior_flags,
  contract_status, gradeable, ungradeable_modules, evidence_rejected,
  model_fingerprint,
  top_strength, top_weakness, top_recommendation, notes, raw_response
)
SELECT $1::uuid, i.agent_id, '1.0',
  d->'pass2'->>'prompt_version', d->>'rubric_version', d->'pass2'->>'model',
  'call_transcript'::input_type,
  (d->>'asr_confidence')::numeric,
  (d->'pass2'->>'final_score')::numeric,
  d->'pass2'->>'performance_level',
  (d->'pass2'->>'weight_applied')::numeric,
  (d->'pass2'->'modules'->>'module1_reception')::numeric,
  (d->'pass2'->'modules'->>'module2_offer')::numeric,
  (d->'pass2'->'modules'->>'module3_objections')::numeric,
  (d->'pass2'->'modules'->>'module4_followup')::numeric,
  (d->'pass2'->'modules'->>'module5_closing')::numeric,
  coalesce(d->'pass2'->'payload'->'modules', '{}'::jsonb),
  coalesce(d->'pass2'->'payload'->'evidence', '[]'::jsonb),
  nullif(d->'pass2'->'payload'->>'stage_reached', '')::conversation_stage,
  coalesce(d->'pass2'->'payload'->'behavior_flags', '[]'::jsonb),
  coalesce(nullif(d->'pass2'->>'contract_status', ''), 'ok'),
  coalesce((d->'pass2'->>'gradeable')::boolean, true)
    AND (d->'pass2'->>'final_score') IS NOT NULL,
  CASE WHEN jsonb_typeof(d->'pass2'->'ungradeable_modules') = 'array'
       THEN d->'pass2'->'ungradeable_modules' ELSE '[]'::jsonb END,
  CASE WHEN jsonb_typeof(d->'pass2'->'evidence_rejected') = 'array'
       THEN d->'pass2'->'evidence_rejected' ELSE '[]'::jsonb END,
  nullif(coalesce(d->'pass2'->'usage'->>'system_fingerprint',
                  d->'pass2'->>'system_fingerprint',
                  d->'pass2'->>'model_fingerprint'), ''),
  d->'pass2'->'payload'->'summary'->>'top_strength',
  d->'pass2'->'payload'->'summary'->>'top_weakness',
  d->'pass2'->'payload'->'summary'->>'top_recommendation',
  d->'pass2'->'payload'->>'notes',
  d->'pass2'->'payload'
FROM interactions i, r WHERE i.interaction_id = $1::uuid
ON CONFLICT (interaction_id) DO UPDATE SET
  agent_id           = EXCLUDED.agent_id,
  schema_version     = EXCLUDED.schema_version,
  prompt_version     = EXCLUDED.prompt_version,
  rubric_version     = EXCLUDED.rubric_version,
  model              = EXCLUDED.model,
  input_type         = EXCLUDED.input_type,
  source_quality     = EXCLUDED.source_quality,
  final_score        = EXCLUDED.final_score,
  performance_level  = EXCLUDED.performance_level,
  weight_applied     = EXCLUDED.weight_applied,
  m1_reception       = EXCLUDED.m1_reception,
  m2_offer           = EXCLUDED.m2_offer,
  m3_objections      = EXCLUDED.m3_objections,
  m4_followup        = EXCLUDED.m4_followup,
  m5_closing         = EXCLUDED.m5_closing,
  breakdown          = EXCLUDED.breakdown,
  evidence           = EXCLUDED.evidence,
  stage_reached      = EXCLUDED.stage_reached,
  behavior_flags     = EXCLUDED.behavior_flags,
  contract_status    = EXCLUDED.contract_status,
  gradeable          = EXCLUDED.gradeable,
  ungradeable_modules = EXCLUDED.ungradeable_modules,
  evidence_rejected  = EXCLUDED.evidence_rejected,
  model_fingerprint  = EXCLUDED.model_fingerprint,
  top_strength       = EXCLUDED.top_strength,
  top_weakness       = EXCLUDED.top_weakness,
  top_recommendation = EXCLUDED.top_recommendation,
  notes              = EXCLUDED.notes,
  raw_response       = EXCLUDED.raw_response,
  updated_at         = now()
RETURNING interaction_id;
