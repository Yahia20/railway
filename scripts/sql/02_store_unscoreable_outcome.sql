-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Store unscoreable outcome'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $('Prepare evaluation input').item.json.interaction_id, JSON.stringify(Object.assign({}, $('Two AI passes').item.json, { asr_confidence: $('Prepare evaluation input').item.json.asr_confidence })), $('Claim work').item.json.uniqueid, $('Claim work').item.json.claim_token ] }}
-- THE UNSCOREABLE OUTCOME IS AN OUTCOME, AND IT IS STORED (round-4 review).
-- The worker refused BEFORE pass 1: the transcript held less speech than the
-- scoring minimum, so /evaluate short-circuited and returned a 200 with a
-- complete pass2 refusal block and NO pass1. The previous revision routed that
-- straight to 'Mark unscoreable' and wrote no evaluation row at all, which
-- meant `unscoreable_count` was never populated by this workflow and the WORST
-- input-quality cases -- the ones most worth seeing -- were the only ones
-- invisible to both reporting views.
--
-- The worker deliberately returns a STORABLE refusal: rubric_version,
-- prompt_version, a named model placeholder, contract_status 'unscoreable',
-- gradeable false, final_score null and the reason in warnings[0]
-- (services/worker/app/main.py, _unscoreable()). Storage policy must not depend
-- on whether pass 1 happened to run, so it is stored here exactly as the
-- pass-1-succeeded variant would be stored by 'Store evaluation'.
--
-- ONE ROW, THEN TERMINAL. This node writes the row; 'Unscoreable stored?' turns
-- a zero-row fencing result into a hard stop; 'Mark unscoreable' then
-- dead-letters the job with no retry. The order matters: a job marked terminal
-- first and crashed before the row was written would be a dead_letter with no
-- evaluation, which is the state this node exists to remove.
--
-- NOTHING SCORE-SHAPED IS WRITTEN. final_score, performance_level and every
-- module score are explicitly null, breakdown/evidence/behaviour flags are
-- empty, and gradeable is computed by the SAME expression 'Store evaluation'
-- uses (what the worker said AND a score actually arrived), which is false here
-- by construction. That keeps the invariant 014's backfill establishes:
-- gradeable implies final_score IS NOT NULL.
--
-- THE VERSION CO-ORDINATE IS THE RESPONSE'S OWN, never a guess. prompt_version,
-- rubric_version and model are NOT NULL columns, so a worker that sent none
-- would abort the statement mid-batch; they fall back to a self-describing
-- 'unknown (...)' label instead, which cannot be mistaken for a real version
-- and cannot silently merge this row into a real version group.
--
-- ON CONFLICT REFRESHES EVERYTHING DERIVED FROM THIS RESPONSE, including
-- clearing the score columns. A re-evaluation that comes back unscoreable
-- against an interaction that previously scored means the stored transcript no
-- longer holds enough speech to grade; the honest record is the current one,
-- with the status column saying why there is no number. Leaving the old module
-- scores beside a null final_score would be the half-one-run-half-another row
-- that 'Store evaluation' documents at length. This is the one destructive edge
-- of storing the outcome, and it is recorded in docs/PR2-db-status.md section 5.
--
-- FENCED AND LOCKED, like every other durable write here: `r` selects FROM
-- lease, the lease CTE takes the job row with FOR UPDATE and requires the lease
-- to still be LIVE (claim_until > now()), and AS MATERIALIZED pins the CTE so
-- it cannot be inlined into the readers. Two orderings, both safe:
--
--   * recovery gets the lock first -> this statement BLOCKS, and on release
--     Postgres re-evaluates the qualification against the NEW row version
--     (EvalPlanQual). The token is gone, the CTE yields no row, nothing is
--     written and the node returns zero rows.
--   * this statement gets the lock first -> recovery BLOCKS until we commit,
--     then re-checks its own predicate against what we wrote.
--
-- Status is 'evaluating' only: this path is reached exclusively after
-- 'Begin judge attempt', which is what put the row into that status.
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
  coalesce(nullif(d->'pass2'->>'prompt_version', ''), 'unknown (worker sent no prompt_version)'),
  coalesce(nullif(d->>'rubric_version', ''),         'unknown (worker sent no rubric_version)'),
  coalesce(nullif(d->'pass2'->>'model', ''),         'none (refused before any model call)'),
  'call_transcript'::input_type,
  (d->>'asr_confidence')::numeric,
  NULL::numeric,                                     -- final_score: there is none
  NULL::text,                                        -- performance_level: likewise
  (d->'pass2'->>'weight_applied')::numeric,
  NULL::numeric, NULL::numeric, NULL::numeric, NULL::numeric, NULL::numeric,
  '{}'::jsonb, '[]'::jsonb, NULL::conversation_stage, '[]'::jsonb,
  coalesce(nullif(d->'pass2'->>'contract_status', ''), 'unscoreable'),
  coalesce((d->'pass2'->>'gradeable')::boolean, true)
    AND (d->'pass2'->>'final_score') IS NOT NULL,
  CASE WHEN jsonb_typeof(d->'pass2'->'ungradeable_modules') = 'array'
       THEN d->'pass2'->'ungradeable_modules' ELSE '[]'::jsonb END,
  CASE WHEN jsonb_typeof(d->'pass2'->'evidence_rejected') = 'array'
       THEN d->'pass2'->'evidence_rejected' ELSE '[]'::jsonb END,
  nullif(coalesce(d->'pass2'->'usage'->>'system_fingerprint',
                  d->'pass2'->>'system_fingerprint',
                  d->'pass2'->>'model_fingerprint'), ''),
  NULL::text, NULL::text, NULL::text,
  coalesce(nullif(d->'pass2'->'warnings'->>0, ''),
           'pass 2 returned contract_status unscoreable with no stated reason'),
  coalesce(d->'pass2'->'payload', '{}'::jsonb)
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
RETURNING interaction_id, contract_status, notes AS unscoreable_reason;
