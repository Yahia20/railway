-- 023 — REMOVE CALLS.
--
-- READ THIS BEFORE RUNNING IT. It deletes 830 call transcripts, 1,064 call
-- ingest jobs and every evaluation attached to them, and it drops three tables.
-- None of it can be undone from inside the database.
--
--   PREREQUISITE, NOT A SUGGESTION:
--       python scripts/dump_calls.py --out ../travelgate-calls-archive/data
--   Run it, check the row counts it prints against section 9 below, and only
--   then run this file. The archive is the only copy afterwards.
--
-- WHY. Calls were never scoreable per agent. All 1,119 recordings decode to
-- extension 3009, which is a QUEUE and not a person, so `agent_evaluations.
-- agent_id` was NULL on every call and always would be. The result was 834
-- evaluations of which roughly 800 could not be attributed to anyone,
-- aggregated under a heading about agent performance. Shelving the schedule
-- would not have fixed that: every view above them still counted them.
--
-- WHAT IS KEPT, AND WHY IT LOOKS ODD. `v_agent_scorecard.calls` stays as a
-- column and is hard-wired to 0; `v_quality_by_input.diarization` stays and is
-- always NULL. Both could have been dropped. They are not, because the display
-- layer (022) and `app/report.py` select them by name, and a column list that
-- changes shape turns a two-view migration into a four-file one for no gain.
-- A reader sees 0 calls, which is true.
--
-- THE ENUM VALUES SURVIVE. `channel.phone_call` and `input_type.call_transcript`
-- cannot be removed without rebuilding both types and every column that uses
-- them, which would rewrite `interactions` and `agent_evaluations` under a
-- lock. Section 8 adds CHECK constraints instead, so the values still exist and
-- cannot be used. That is the same guarantee at a fraction of the risk.
--
-- ORDER MATTERS. Views that read `transcripts` are rebuilt BEFORE the table is
-- dropped, and call ROWS are deleted before the tables they cascade from.
--
-- RESTORING: the calls archive holds every dropped object's definition under
-- `db/migrations/` and every edited file under `shared-before/`.

BEGIN;

-- A DROP COLUMN on `interactions` fights live ingest. Fail fast and re-run
-- rather than deadlock a webhook that is taking real customer messages.
SET lock_timeout = '5s';
SET statement_timeout = '10min';

-- ---------------------------------------------------------------------------
-- 1 · The eligibility rule has nothing left to exclude
--
-- `eval_asr_input_is_eligible(input_type, asr_quality_status)` published a chat
-- always and a call only on a green transcript. With no calls it returns true
-- for every row, so it is dropped rather than left as a function that always
-- says yes — which is the kind of thing somebody later mistakes for a live
-- gate. Every caller is rebuilt below.
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS v_quality_by_input_display;
DROP VIEW IF EXISTS v_agent_scorecard_display;
DROP VIEW IF EXISTS v_quality_by_input;
DROP VIEW IF EXISTS v_agent_scorecard;
DROP VIEW IF EXISTS v_usable_evaluations;
DROP FUNCTION IF EXISTS eval_asr_input_is_eligible(input_type, text);

-- ---------------------------------------------------------------------------
-- 2 · v_usable_evaluations — the D1 join goes, the usability rule stays
-- ---------------------------------------------------------------------------

CREATE VIEW v_usable_evaluations AS
SELECT e.*
FROM agent_evaluations e
WHERE eval_score_is_usable(e.contract_status, e.gradeable, e.final_score);

COMMENT ON VIEW v_usable_evaluations IS
  'agent_evaluations restricted to rows carrying a score anybody may average AND publish. The ASR-quality half of that rule went with the calls lane in 023: there is no transcript left to be amber. A view expands * at creation time, so re-run this after any migration that adds a column to agent_evaluations.';

-- ---------------------------------------------------------------------------
-- 3 · v_agent_scorecard — same shape, one channel
-- ---------------------------------------------------------------------------

CREATE VIEW v_agent_scorecard AS
-- src   every evaluation the scorecard can see, with the two gates resolved
--       once as columns. One join block, three consumers: the join written out
--       three times is the join that acquires a fourth condition in one of them.
-- keys  the group universe, taken BEFORE the eligibility filter, so a group
--       whose every row was shadowed still has a row here.
-- agg   the published aggregates, over ELIGIBLE rows only.
-- shad  the excluded rows, counted and not averaged.
WITH src AS (
  SELECT
    a.agent_id,
    a.full_name,
    a.team,

    -- The version co-ordinates. Two rows are comparable only if all four match.
    e.prompt_version,
    e.rubric_version,
    e.model,
    e.model_fingerprint,

    e.contract_status,
    e.final_score,
    e.m1_reception,
    e.m2_offer,
    e.m3_objections,
    e.m4_followup,
    e.m5_closing,
    e.behavior_flags,
    i.channel,
    m.first_response_seconds,

    u.usable,
    x.asr_eligible
  FROM agent_evaluations e
  CROSS JOIN LATERAL (
    SELECT eval_score_is_usable(e.contract_status, e.gradeable, e.final_score) AS usable
  ) u
  JOIN interactions i ON i.interaction_id = e.interaction_id
  JOIN agents a       ON a.agent_id = e.agent_id
  LEFT JOIN interaction_metrics m ON m.interaction_id = e.interaction_id
  -- interaction_id is NOT NULL UNIQUE (003_interactions.sql), so there is no
  -- evaluation out into several rows. A LEFT join, not an inner one, because a
  CROSS JOIN LATERAL (
    -- Every surviving evaluation is a chat, and a chat was always
    -- eligible. Kept as a column so the shadow/published split below and
    -- the display layer on top of it need no edits at all.
    SELECT true AS asr_eligible
  ) x
  -- Bot-handled conversations are excluded: the bot qualifies customers before
  -- a human joins, and counting its conversations against human agents makes
  -- every QA number wrong. Rows with a NULL agent_id are excluded too, by the
  -- inner join to agents. Preflight P2b measures exactly this population, so
  -- the reconciliation compares like with like.
  WHERE a.is_bot = false
),
keys AS (
  SELECT DISTINCT
    agent_id, full_name, team,
    prompt_version, rubric_version, model, model_fingerprint
  FROM src
),
agg AS (
  SELECT
    src.agent_id,
    src.prompt_version,
    src.rubric_version,
    src.model,
    src.model_fingerprint,

    -- Population.
    count(*)                                            AS evaluated_interactions,
    0::bigint                                           AS calls,
    count(*)                                            AS chats,

    -- The five buckets. They partition evaluated_interactions.
    count(src.final_score) FILTER (WHERE src.usable)    AS scored_interactions,
    count(*) FILTER (WHERE src.contract_status = 'ungradeable')     AS ungradeable_count,
    count(*) FILTER (WHERE src.contract_status = 'unscoreable')     AS unscoreable_count,
    count(*) FILTER (WHERE src.contract_status = 'contract_failed') AS contract_failed_count,
    -- Not graded, reason not recorded: pre-013 history, and any writer that
    -- still does not send a status. Should be flat after rollout.
    count(*) FILTER (WHERE src.contract_status = 'ok' AND NOT src.usable)
                                                        AS ok_without_score_count,

    -- The statistical N behind every average below. Equal to
    -- scored_interactions by construction; kept as its own column because one
    -- is a reporting count and the other is the N the interval is computed
    -- from, and a reader must not have to know they are the same.
    count(*) FILTER (WHERE src.usable)                  AS n_usable,

    -- Averages, over usable rows ONLY. Unrounded here on purpose: the band gate
    -- compares mean +/- half-width against a boundary, and rounding before that
    -- comparison is how a mean lands on the wrong side of 85.
    avg(src.final_score)      FILTER (WHERE src.usable) AS mean_score,
    -- The observed between-call spread: the half of the uncertainty the first
    -- draft left out entirely.
    var_samp(src.final_score) FILTER (WHERE src.usable) AS sample_var,
    avg(src.m1_reception)     FILTER (WHERE src.usable) AS mean_reception,
    avg(src.m2_offer)         FILTER (WHERE src.usable) AS mean_offer,
    avg(src.m3_objections)    FILTER (WHERE src.usable) AS mean_objections,
    avg(src.m4_followup)      FILTER (WHERE src.usable) AS mean_followup,
    avg(src.m5_closing)       FILTER (WHERE src.usable) AS mean_closing,
    -- A module scored on very few conversations is not a signal. Surface the n
    -- alongside the mean so nobody coaches against three data points.
    count(src.m5_closing)     FILTER (WHERE src.usable) AS n_closing_scored,

    avg(src.first_response_seconds)                     AS mean_first_response_sec,
    count(*) FILTER (WHERE src.behavior_flags <> '[]'::jsonb) AS flagged_conversations
  FROM src
  -- SOL'S D1 RULE. Everything above this line describes GREEN CALLS AND CHATS
  -- and nothing else. Applied as a WHERE and not as one more FILTER on each of
  -- the eighteen aggregates above, for the reason stated at the top of this
  -- view: eighteen copies of a gate is seventeen chances to forget one.
  WHERE src.asr_eligible
  GROUP BY src.agent_id, src.prompt_version, src.rubric_version,
           src.model, src.model_fingerprint
),
shad AS (
  -- The shadow population: what the rule above threw away, so the exclusion is
  -- a number on the page instead of a silence. NOT averaged, and deliberately
  -- not broken down by status -- if you want to know how amber scores compare
  -- to green ones, that is a shadow ANALYSIS and it reads agent_evaluations
  -- directly. This column exists to answer "how much of this agent's month is
  -- missing from the row I am looking at".
  SELECT
    src.agent_id,
    src.prompt_version,
    src.rubric_version,
    src.model,
    src.model_fingerprint,
    count(*)                                        AS amber_shadow_count,
    -- Of those, the ones that WOULD have been averaged but for the ASR gate.
    -- This is the number that moves an agent's mean if somebody re-transcribes
    -- the audio and the status turns green; the difference between the two
    -- columns is rows that were never gradeable anyway.
    count(*) FILTER (WHERE src.usable)              AS amber_shadow_usable_count
  FROM src
  WHERE NOT src.asr_eligible
  GROUP BY src.agent_id, src.prompt_version, src.rubric_version,
           src.model, src.model_fingerprint
)
SELECT
  k.agent_id,
  k.full_name,
  k.team,
  k.prompt_version,
  k.rubric_version,
  k.model,
  k.model_fingerprint,

  -- coalesce to 0, not NULL: a group every one of whose rows was shadowed still
  -- appears, reading 0 evaluated / N shadowed. A row that reads zero is a fact;
  -- a row that vanished is a fact nobody sees.
  coalesce(agg.evaluated_interactions, 0) AS evaluated_interactions,
  coalesce(agg.calls,                  0) AS calls,
  coalesce(agg.chats,                  0) AS chats,

  coalesce(agg.scored_interactions,    0) AS scored_interactions,
  coalesce(agg.ungradeable_count,      0) AS ungradeable_count,
  coalesce(agg.unscoreable_count,      0) AS unscoreable_count,
  coalesce(agg.contract_failed_count,  0) AS contract_failed_count,
  coalesce(agg.ok_without_score_count, 0) AS ok_without_score_count,
  coalesce(agg.n_usable,               0) AS n_usable,

  -- THE EXCLUSION, MADE VISIBLE. Call evaluations dropped by the D1 rule:
  -- all. They are NOT part of the five-bucket partition above -- the partition
  -- covers evaluated_interactions, and these rows are not in it.
  coalesce(shad.amber_shadow_count,        0) AS amber_shadow_count,
  coalesce(shad.amber_shadow_usable_count, 0) AS amber_shadow_usable_count,

  round(agg.mean_score,      1) AS avg_score,
  round(agg.mean_reception,  1) AS avg_reception,
  round(agg.mean_offer,      1) AS avg_offer,
  round(agg.mean_objections, 1) AS avg_objections,
  round(agg.mean_followup,   1) AS avg_followup,
  round(agg.mean_closing,    1) AS avg_closing,
  coalesce(agg.n_closing_scored, 0) AS n_closing_scored,

  -- Uncertainty, with both halves visible. noise_variance NULL means this
  -- co-ordinate has no measured noise floor: nothing about it is publishable
  -- until somebody re-runs the A/A on it and INSERTs the result.
  nv.noise_variance,
  round(agg.sample_var, 1)      AS score_sample_variance,
  hw.noise_floor_half_width,
  hw.ci95_half_width,
  round(agg.mean_score - hw.ci95_half_width, 1) AS score_ci_low,
  round(agg.mean_score + hw.ci95_half_width, 1) AS score_ci_high,

  eval_performance_band(agg.mean_score) AS band,

  -- THE PUBLICATION GATE. A band may be shown only when there are enough usable
  -- scores AND the whole COMPLETE interval sits inside one band. Anything else
  -- is a coin flip presented as a grade: 11 of 68 bands flipped in the A/A run
  -- with no prompt change at all.
  --
  -- Nothing about the D1 rule is restated here, and nothing needs to be: the
  -- amber rows never entered agg, so n_usable, the mean and the interval are
  -- already green-only.
  --
  -- coalesce(..., false): a missing min_n_publish row makes the comparison
  -- NULL, and NULL is not false. Fail closed.
  coalesce(
    (
          agg.n_usable >= eval_report_param('min_n_publish')
      AND agg.mean_score     IS NOT NULL
      AND hw.ci95_half_width IS NOT NULL
      AND eval_performance_band(agg.mean_score - hw.ci95_half_width)
        = eval_performance_band(agg.mean_score + hw.ci95_half_width)
    ), false) AS band_stable,

  round(agg.mean_first_response_sec) AS avg_first_response_sec,
  coalesce(agg.flagged_conversations, 0) AS flagged_conversations
FROM keys k
-- agent_id, prompt_version, rubric_version and model are NOT NULL in their
-- source tables, so plain equality is safe on those four. model_fingerprint is
-- nullable -- the worker did not always capture one -- and `= NULL` would drop
-- exactly the historical groups this view most needs to show, so it joins with
-- IS NOT DISTINCT FROM. full_name and team are not join keys: they are
-- functionally dependent on agent_id, which is the agents primary key.
LEFT JOIN agg
       ON agg.agent_id          =                    k.agent_id
      AND agg.prompt_version    =                    k.prompt_version
      AND agg.rubric_version    =                    k.rubric_version
      AND agg.model             =                    k.model
      AND agg.model_fingerprint IS NOT DISTINCT FROM k.model_fingerprint
LEFT JOIN shad
       ON shad.agent_id          =                    k.agent_id
      AND shad.prompt_version    =                    k.prompt_version
      AND shad.rubric_version    =                    k.rubric_version
      AND shad.model             =                    k.model
      AND shad.model_fingerprint IS NOT DISTINCT FROM k.model_fingerprint
CROSS JOIN LATERAL (
  SELECT eval_noise_param('repeat_run_variance', k.prompt_version,
                          k.rubric_version, k.model, k.model_fingerprint)
           AS noise_variance
) nv
CROSS JOIN LATERAL (
  SELECT eval_noise_floor_half_width_95(agg.n_usable, nv.noise_variance)
           AS noise_floor_half_width,
         eval_ci_half_width_95(agg.n_usable, agg.sample_var, nv.noise_variance)
           AS ci95_half_width
) hw;
;

COMMENT ON VIEW v_agent_scorecard IS
  'One row per agent per (prompt_version, rubric_version, model, model_fingerprint). `calls` is 0 by construction since 023 removed the calls lane, and `chats` is the whole population. Never average across rows from different version co-ordinates.';

-- ---------------------------------------------------------------------------
-- 4 · v_quality_by_input — diarization and confidence were transcript facts
--
-- Both columns survive as constants so the display layer and the worker need
-- no edit. `confidence_bucket` becomes width_bucket(1.0, 0, 1, 5) = 6, which is
-- the value every chat row already carried, so no existing report output moves.
-- ---------------------------------------------------------------------------

CREATE VIEW v_quality_by_input AS
-- Same three-part shape as v_agent_scorecard, and for the same reasons: one
-- join block, a group universe taken before the eligibility filter, aggregates
-- over eligible rows only, and the excluded rows counted beside them.
WITH src AS (
  SELECT
    e.input_type,
    NULL::text AS diarization,
    width_bucket(1.0, 0, 1, 5) AS confidence_bucket,

    e.prompt_version,
    e.rubric_version,
    e.model,
    e.model_fingerprint,

    e.contract_status,
    e.final_score,

    u.usable,
    x.asr_eligible
  FROM agent_evaluations e
  CROSS JOIN LATERAL (
    SELECT eval_score_is_usable(e.contract_status, e.gradeable, e.final_score) AS usable
  ) u
  -- UNIQUE, 003_interactions.sql), so this join cannot fan a row out and there
  CROSS JOIN LATERAL (
    -- Every surviving evaluation is a chat, and a chat was always
    -- eligible. Kept as a column so the shadow/published split below and
    -- the display layer on top of it need no edits at all.
    SELECT true AS asr_eligible
  ) x
),
keys AS (
  SELECT DISTINCT
    input_type, diarization, confidence_bucket,
    prompt_version, rubric_version, model, model_fingerprint
  FROM src
),
agg AS (
  SELECT
    src.input_type,
    src.diarization,
    src.confidence_bucket,
    src.prompt_version,
    src.rubric_version,
    src.model,
    src.model_fingerprint,

    count(*)                                              AS n,
    count(*) FILTER (WHERE src.usable)                    AS n_usable,
    count(*) FILTER (WHERE src.contract_status = 'ungradeable')     AS ungradeable_count,
    count(*) FILTER (WHERE src.contract_status = 'unscoreable')     AS unscoreable_count,
    count(*) FILTER (WHERE src.contract_status = 'contract_failed') AS contract_failed_count,
    count(*) FILTER (WHERE src.contract_status = 'ok' AND NOT src.usable)
                                                          AS ok_without_score_count,

    avg(src.final_score)        FILTER (WHERE src.usable) AS mean_score,
    var_samp(src.final_score)   FILTER (WHERE src.usable) AS sample_var,
    stddev_pop(src.final_score) FILTER (WHERE src.usable) AS score_spread_pop
  FROM src
  -- SOL'S D1 RULE, the same one v_agent_scorecard applies, so the two views
  -- cannot come to different conclusions about which calls count.
  WHERE src.asr_eligible
  GROUP BY src.input_type, src.diarization, src.confidence_bucket,
           src.prompt_version, src.rubric_version, src.model,
           src.model_fingerprint
),
shad AS (
  SELECT
    src.input_type,
    src.diarization,
    src.confidence_bucket,
    src.prompt_version,
    src.rubric_version,
    src.model,
    src.model_fingerprint,
    count(*)                           AS amber_shadow_count,
    count(*) FILTER (WHERE src.usable) AS amber_shadow_usable_count
  FROM src
  WHERE NOT src.asr_eligible
  GROUP BY src.input_type, src.diarization, src.confidence_bucket,
           src.prompt_version, src.rubric_version, src.model,
           src.model_fingerprint
)
SELECT
  k.input_type,
  k.diarization,
  k.confidence_bucket,
  k.prompt_version,
  k.rubric_version,
  k.model,
  k.model_fingerprint,

  coalesce(agg.n,                      0) AS n,
  coalesce(agg.n_usable,               0) AS n_usable,
  coalesce(agg.ungradeable_count,      0) AS ungradeable_count,
  coalesce(agg.unscoreable_count,      0) AS unscoreable_count,
  coalesce(agg.contract_failed_count,  0) AS contract_failed_count,
  coalesce(agg.ok_without_score_count, 0) AS ok_without_score_count,

  -- The rows the D1 rule removed from `n`. THIS IS THE COLUMN THAT KEEPS THIS
  -- VIEW HONEST after the rule: `n` no longer contains the bad-ASR calls, so
  -- without this the view would answer "does the model score calls worse than
  -- chats" having quietly deleted the worst calls from the question. Read the
  -- three together: `n` seen, `n_usable` averaged, `amber_shadow_count` never
  -- offered.
  coalesce(shad.amber_shadow_count,        0) AS amber_shadow_count,
  coalesce(shad.amber_shadow_usable_count, 0) AS amber_shadow_usable_count,

  round(agg.mean_score, 1)       AS avg_score,
  round(agg.score_spread_pop, 1) AS score_spread,

  nv.noise_variance,
  round(agg.sample_var, 1)       AS score_sample_variance,
  hw.noise_floor_half_width,
  hw.ci95_half_width,
  round(agg.mean_score - hw.ci95_half_width, 1) AS score_ci_low,
  round(agg.mean_score + hw.ci95_half_width, 1) AS score_ci_high,

  coalesce(
    (
          agg.n_usable >= eval_report_param('min_n_publish')
      AND agg.mean_score     IS NOT NULL
      AND hw.ci95_half_width IS NOT NULL
      AND eval_performance_band(agg.mean_score - hw.ci95_half_width)
        = eval_performance_band(agg.mean_score + hw.ci95_half_width)
    ), false) AS band_stable
FROM keys k
-- input_type, prompt_version, rubric_version and model are NOT NULL in their
-- source tables; diarization, confidence_bucket and model_fingerprint can all
-- three join with IS NOT DISTINCT FROM.
LEFT JOIN agg
       ON agg.input_type        =                    k.input_type
      AND agg.diarization       IS NOT DISTINCT FROM k.diarization
      AND agg.confidence_bucket IS NOT DISTINCT FROM k.confidence_bucket
      AND agg.prompt_version    =                    k.prompt_version
      AND agg.rubric_version    =                    k.rubric_version
      AND agg.model             =                    k.model
      AND agg.model_fingerprint IS NOT DISTINCT FROM k.model_fingerprint
LEFT JOIN shad
       ON shad.input_type        =                    k.input_type
      AND shad.diarization       IS NOT DISTINCT FROM k.diarization
      AND shad.confidence_bucket IS NOT DISTINCT FROM k.confidence_bucket
      AND shad.prompt_version    =                    k.prompt_version
      AND shad.rubric_version    =                    k.rubric_version
      AND shad.model             =                    k.model
      AND shad.model_fingerprint IS NOT DISTINCT FROM k.model_fingerprint
CROSS JOIN LATERAL (
  SELECT eval_noise_param('repeat_run_variance', k.prompt_version,
                          k.rubric_version, k.model, k.model_fingerprint)
           AS noise_variance
) nv
CROSS JOIN LATERAL (
  SELECT eval_noise_floor_half_width_95(agg.n_usable, nv.noise_variance)
           AS noise_floor_half_width,
         eval_ci_half_width_95(agg.n_usable, agg.sample_var, nv.noise_variance)
           AS ci95_half_width
) hw;
;

COMMENT ON VIEW v_quality_by_input IS
  'Score quality by input co-ordinate. Since 023 there is one input type: diarization is always NULL and confidence_bucket always 6, both kept as columns so the display layer above needs no change.';

-- ---------------------------------------------------------------------------
-- 5 · The display layer, re-created on the rebuilt views (022, verbatim)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_agent_scorecard_display AS
SELECT s.*,
       eval_confidence_label(s.n_usable, s.band_stable)  AS confidence_label,
       eval_is_provisional(s.n_usable, s.band_stable)    AS is_provisional,
       eval_method_label(s.prompt_version, s.rubric_version,
                         s.model, s.model_fingerprint)   AS method_label,
       jsonb_build_object(
         'value',          s.avg_score,
         'n_usable',       s.n_usable,
         'label',          eval_confidence_label(s.n_usable, s.band_stable),
         'is_provisional', eval_is_provisional(s.n_usable, s.band_stable),
         'band',           s.band,
         'band_stable',    s.band_stable,
         'method',         eval_method_label(s.prompt_version, s.rubric_version,
                                             s.model, s.model_fingerprint),
         -- One string that is safe to render on its own. Everything a reader
         -- needs in order not to over-read the number is already in it.
         'headline',
           CASE WHEN s.avg_score IS NULL
                THEN 'no score yet'
                ELSE s.avg_score::text || ' ('
                     || eval_confidence_label(s.n_usable, s.band_stable) || ')'
           END
       ) AS score_display
FROM v_agent_scorecard s;

COMMENT ON VIEW v_agent_scorecard_display IS
  'v_agent_scorecard plus the labels a number must never be shown without. READ THIS ONE FOR ANY HUMAN-FACING REPORT. The base view stays the analytical surface and is unchanged. A mean here is ALWAYS present and ALWAYS accompanied by n_usable and confidence_label; is_provisional is false only for a fully published figure, and any automated consumer must gate on it.';


CREATE OR REPLACE VIEW v_quality_by_input_display AS
SELECT q.*,
       eval_confidence_label(q.n_usable, q.band_stable)  AS confidence_label,
       eval_is_provisional(q.n_usable, q.band_stable)    AS is_provisional,
       eval_method_label(q.prompt_version, q.rubric_version,
                         q.model, q.model_fingerprint)   AS method_label,
       jsonb_build_object(
         'value',          q.avg_score,
         'n_usable',       q.n_usable,
         'label',          eval_confidence_label(q.n_usable, q.band_stable),
         'is_provisional', eval_is_provisional(q.n_usable, q.band_stable),
         'band_stable',    q.band_stable,
         'method',         eval_method_label(q.prompt_version, q.rubric_version,
                                             q.model, q.model_fingerprint),
         'headline',
           CASE WHEN q.avg_score IS NULL
                THEN 'no score yet'
                ELSE q.avg_score::text || ' ('
                     || eval_confidence_label(q.n_usable, q.band_stable) || ')'
           END
       ) AS score_display
FROM v_quality_by_input q;

COMMENT ON VIEW v_quality_by_input_display IS
  'v_quality_by_input plus the same labels. READ THIS ONE FOR ANY HUMAN-FACING REPORT. Answers "does the model score calls worse than chats, or is the transcript just bad" with the sample size and the interim-method caveat attached to every mean.';

-- (022's own COMMIT was removed when its text was carried in here: it would
--  have split this migration in two, and the half that failed would have
--  been the half that deletes rows.)


-- ---------------------------------------------------------------------------
-- 6 · evaluate_alert_rules() — the ASR quality it read no longer exists
--
-- The function joined `transcripts` for one value and defaulted it to 'green'.
-- Rather than re-declare 200 lines of rule logic here and risk it drifting from
-- 013, this patches the two lines in the LIVE definition. Both are unique in
-- the body, and the block refuses to install anything that still mentions the
-- dropped table.
-- ---------------------------------------------------------------------------

DO $patch$
DECLARE
  src text;
BEGIN
  SELECT pg_get_functiondef(p.oid) INTO src
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE p.proname = 'evaluate_alert_rules' AND n.nspname = 'public';

  IF src IS NULL THEN
    RAISE EXCEPTION 'evaluate_alert_rules() not found - nothing to patch';
  END IF;

  IF position('transcripts' in src) = 0 THEN
    RAISE NOTICE 'evaluate_alert_rules() already free of transcripts; skipping';
    RETURN;
  END IF;

  src := replace(src,
    '  LEFT JOIN transcripts          t  ON t.interaction_id  = i.interaction_id' || chr(10),
    '');
  src := replace(src,
    'coalesce(t.asr_metrics->>''asr_quality_status'', ''green'') AS asr_quality_status',
    '''green''::text AS asr_quality_status');

  IF position('transcripts' in src) > 0 THEN
    RAISE EXCEPTION 'evaluate_alert_rules() still references transcripts after patching - the body has drifted from 013; edit it by hand';
  END IF;

  EXECUTE src;
END
$patch$;

-- ---------------------------------------------------------------------------
-- 7 · The rows
--
-- `interactions` cascades to chat_messages, interaction_analysis,
-- agent_evaluations, interaction_requests, interaction_metrics and
-- alert_occurrences, so deleting the call conversations takes their whole
-- subtree with them. The two things that do NOT cascade are deleted first.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- 7a · The spend views metered ASR as well as the judge
--
-- `v_spend_mtd` and `v_spend_by_component` read `asr_runs`, and `v_pipeline_gate`
-- reads `v_spend_mtd`, so the table cannot be dropped while they stand. Rebuilt
-- here over `model_calls` alone — the only thing left that spends money.
--
-- THE COLUMN LISTS DO NOT CHANGE. `app/budget.py`, the /spend page and the
-- report all select these by name, and `v_pipeline_gate` is what every workflow
-- asks before it claims work. A column that disappears here takes the budget
-- gate down with it, and a pipeline whose gate errors is a pipeline that stops.
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS v_pipeline_gate;
DROP VIEW IF EXISTS v_spend_by_component;
DROP VIEW IF EXISTS v_spend_mtd;

CREATE VIEW v_spend_mtd AS
WITH bounds AS (
  SELECT date_trunc('month', now() AT TIME ZONE 'Asia/Riyadh')
           AT TIME ZONE 'Asia/Riyadh' AS month_start
),
judge AS (
  SELECT 'deepseek'::text            AS provider,
         coalesce(sum(cost_usd), 0)  AS spend_usd,
         count(*)                    AS units
  FROM model_calls, bounds
  WHERE created_at >= bounds.month_start
)
SELECT b.provider,
       b.display_name,
       b.monthly_cap_usd,
       b.hard_stop,
       b.enabled,
       coalesce(s.spend_usd, 0)                          AS spend_mtd_usd,
       coalesce(s.units, 0)                              AS units_mtd,
       CASE WHEN b.monthly_cap_usd IS NULL THEN NULL
            ELSE greatest(b.monthly_cap_usd - coalesce(s.spend_usd, 0), 0)
       END                                               AS remaining_usd,
       CASE WHEN b.monthly_cap_usd IS NULL OR b.monthly_cap_usd = 0 THEN NULL
            ELSE round(100 * coalesce(s.spend_usd, 0) / b.monthly_cap_usd, 1)
       END                                               AS pct_of_cap,
       (b.monthly_cap_usd IS NOT NULL
        AND coalesce(s.spend_usd, 0) >= b.monthly_cap_usd)  AS over_cap
FROM provider_budgets b
LEFT JOIN judge s ON s.provider = b.provider;

COMMENT ON VIEW v_spend_mtd IS
  'Spend this calendar month per provider against its cap, metered from model_calls. The asr_runs half went with the calls lane in 023. `over_cap` is what the hard stop reads.';

CREATE VIEW v_pipeline_gate AS
SELECT s.provider,
       s.display_name,
       s.enabled,
       s.spend_mtd_usd,
       s.monthly_cap_usd,
       s.remaining_usd,
       st.balance_usd,
       st.checked_at,
       CASE
         WHEN NOT s.enabled THEN false
         WHEN s.hard_stop AND s.over_cap THEN false
         WHEN b.require_positive_balance
              AND st.available IS NOT DISTINCT FROM false THEN false
         -- Fails CLOSED for a provider whose balance we CAN check and have not.
         -- An unchecked DeepSeek is the state that would have emptied the queue.
         WHEN st.provider IS NULL AND b.require_positive_balance THEN false
         ELSE true
       END AS may_run,
       CASE
         WHEN NOT s.enabled
           THEN 'disabled in provider_budgets'
         WHEN s.hard_stop AND s.over_cap
           THEN format('monthly cap reached: %s of %s USD spent',
                       round(s.spend_mtd_usd, 2), s.monthly_cap_usd)
         WHEN b.require_positive_balance
              AND st.available IS NOT DISTINCT FROM false
           THEN coalesce(st.reason, 'provider reports no available balance')
         WHEN st.provider IS NULL AND b.require_positive_balance
           THEN 'no preflight has run yet - balance unknown'
         ELSE NULL
       END AS reason
FROM v_spend_mtd s
JOIN provider_budgets b ON b.provider = s.provider
LEFT JOIN provider_status st ON st.provider = s.provider;

COMMENT ON VIEW v_pipeline_gate IS
  'May this provider be used right now, and if not, why. The only gate a workflow should read. Fails CLOSED for a provider whose balance we can check and have not.';

CREATE VIEW v_spend_by_component AS
WITH bounds AS (
  SELECT date_trunc('month', now() AT TIME ZONE 'Asia/Riyadh')
           AT TIME ZONE 'Asia/Riyadh' AS month_start
)
SELECT 'deepseek'                          AS provider,
       mc.purpose                          AS component,
       count(*)                            AS calls,
       count(*) FILTER (WHERE NOT mc.succeeded) AS failed,
       sum(mc.prompt_tokens)               AS prompt_tokens,
       sum(mc.cached_tokens)               AS cached_tokens,
       sum(mc.output_tokens)               AS output_tokens,
       round(sum(mc.cost_usd), 4)          AS spend_usd,
       count(*) FILTER (WHERE mc.priced_at_peak) AS at_peak_rate,
       min(mc.created_at)                  AS first_at,
       max(mc.created_at)                  AS last_at
FROM model_calls mc, bounds
WHERE mc.created_at >= bounds.month_start
GROUP BY mc.purpose;

COMMENT ON VIEW v_spend_by_component IS
  'This month''s spend broken down by the thing that spent it. Each row is independently switchable, which is what makes the number actionable.';

-- ---------------------------------------------------------------------------
-- 7b · The alert views joined call_ingest_jobs for ONE column
--
-- `uniqueid` is the PBX call id, and both views LEFT JOIN the whole jobs table
-- to fetch it. With no calls it is NULL on every row, so the join is dropped
-- and the column is kept as an explicit NULL — same reasoning as
-- `v_agent_scorecard.calls`: the /report page and the alert digest select these
-- by name, and a column list that changes shape turns a schema migration into
-- a front-end one.
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS v_alert_digest_daily;
DROP VIEW IF EXISTS v_alert_queue;

CREATE VIEW v_alert_queue AS
SELECT o.occurrence_id,
       o.rule_code,
       o.rule_version,
       r.description                                   AS rule_description,
       o.created_at,
       o.delivery_status,
       i.interaction_id,
       i.channel,
       i.started_at,
       i.customer_phone_e164,
       coalesce(a.full_name, 'unassigned')             AS agent_name,
       NULL::text                                      AS uniqueid,
       o.fact_snapshot ->> 'summary_ar'                AS summary_ar,
       coalesce(o.fact_snapshot -> 'products', '[]'::jsonb) AS products,
       o.fact_snapshot ->> 'real_ask_quote'            AS real_ask_quote,
       o.fact_snapshot ->> 'lead_temperature'          AS lead_temperature,
       (o.fact_snapshot ->> 'overdue')::boolean        AS promise_overdue,
       o.fact_snapshot
FROM alert_occurrences o
JOIN interactions i   ON i.interaction_id = o.interaction_id
LEFT JOIN alert_rules r ON r.rule_code = o.rule_code
LEFT JOIN agents a    ON a.agent_id = i.agent_id
WHERE o.delivery_status = 'pending'
ORDER BY o.created_at DESC;

COMMENT ON VIEW v_alert_queue IS
  'Alerts nobody has acted on yet, newest first. `uniqueid` is NULL since 023 removed the calls lane; the column is kept so readers of this view need no change.';

CREATE VIEW v_alert_digest_daily AS
SELECT (o.created_at AT TIME ZONE 'Asia/Riyadh')::date  AS alert_day,
       o.rule_code,
       r.description                                    AS rule_description,
       count(*)                                         AS occurrences,
       count(*) FILTER (WHERE o.delivery_status = 'pending')      AS pending,
       count(*) FILTER (WHERE o.delivery_status = 'acknowledged') AS acknowledged,
       count(*) FILTER (WHERE o.delivery_status = 'suppressed')   AS suppressed,
       coalesce(jsonb_agg(jsonb_build_object(
           'occurrence_id',       o.occurrence_id,
           'interaction_id',      i.interaction_id,
           'uniqueid',            NULL,
           'started_at',          i.started_at,
           'customer_phone_e164', i.customer_phone_e164,
           'agent_name',          coalesce(a.full_name, 'unassigned'),
           'summary_ar',          o.fact_snapshot ->> 'summary_ar',
           'products',            coalesce(o.fact_snapshot -> 'products', '[]'::jsonb),
           'real_ask_quote',      o.fact_snapshot ->> 'real_ask_quote',
           'lead_temperature',    o.fact_snapshot ->> 'lead_temperature',
           'promise_text',        o.fact_snapshot ->> 'promise_text',
           'due_at',              o.fact_snapshot ->> 'due_at',
           'overdue',             o.fact_snapshot -> 'overdue')
         ORDER BY i.started_at) FILTER (WHERE o.delivery_status = 'pending'),
         '[]'::jsonb)                                   AS pending_items
FROM alert_occurrences o
JOIN interactions i   ON i.interaction_id = o.interaction_id
LEFT JOIN alert_rules r ON r.rule_code = o.rule_code
LEFT JOIN agents a    ON a.agent_id = i.agent_id
GROUP BY 1, o.rule_code, r.description
ORDER BY 1 DESC, o.rule_code;

COMMENT ON VIEW v_alert_digest_daily IS
  'One row per day per rule, with the pending occurrences inlined for a digest message. `uniqueid` is NULL since 023.';

-- ---------------------------------------------------------------------------
-- 7c · The rows and the tables
-- ---------------------------------------------------------------------------

-- THE TABLES GO FIRST, AND THAT ORDER IS NOT COSMETIC.
--
-- `call_ingest_jobs.interaction_id` references `interactions` WITHOUT
-- ON DELETE CASCADE, so deleting the conversations while that table still
-- exists fails on the foreign key -- measured, on the first real run of this
-- file. Dropping the table takes the reference with it.
--
-- Everything else DOES cascade from `interactions`: chat_messages,
-- interaction_analysis, agent_evaluations, interaction_requests,
-- interaction_metrics and alert_occurrences all go with their conversation.
-- call_ingest_jobs REFERENCES asr_runs (which batch transcribed this job), so
-- the child goes first. Both orderings in this file were wrong on their first
-- real run; neither is guessable from the migration that created them.
DROP TABLE IF EXISTS call_ingest_jobs;
DROP TABLE IF EXISTS asr_runs;
DROP TABLE IF EXISTS transcripts;

DELETE FROM follow_ups
 WHERE promised_in IN (SELECT interaction_id FROM interactions
                        WHERE external_source = 'asterisk_drive');

-- Rule 12 says every judge call lands in `model_calls`, and it is the only
-- measurement of what the system costs. Removing the calls half of that history
-- is deliberate: the remaining rows must describe the remaining pipeline, or
-- the cost-per-conversation on /spend is an average over work that no longer
-- happens. The archive keeps the full table.
DELETE FROM model_calls
 WHERE purpose = 'asr'
    OR interaction_id IN (SELECT interaction_id FROM interactions
                           WHERE external_source = 'asterisk_drive');

DELETE FROM interactions WHERE external_source = 'asterisk_drive';

-- Modal and Cohere paid for transcription and for nothing else.
DELETE FROM provider_status  WHERE provider IN ('modal', 'cohere');
DELETE FROM provider_budgets WHERE provider IN ('modal', 'cohere');

-- ---------------------------------------------------------------------------
-- 8 · The tables, the columns, and the door
-- ---------------------------------------------------------------------------

ALTER TABLE interaction_metrics DROP COLUMN IF EXISTS agent_talk_ratio;
ALTER TABLE interactions        DROP COLUMN IF EXISTS direction;
ALTER TABLE interactions        DROP COLUMN IF EXISTS duration_seconds;

-- The enum values cannot be removed (see the header). These make them
-- unusable, which is the property that actually matters. NOT VALID then
-- VALIDATE takes a weaker lock than a plain ADD CONSTRAINT, and the VALIDATE
-- is what proves no row already breaks it.
ALTER TABLE interactions
  ADD CONSTRAINT interactions_no_calls_ck
  CHECK (channel <> 'phone_call') NOT VALID;
ALTER TABLE interactions VALIDATE CONSTRAINT interactions_no_calls_ck;

ALTER TABLE agent_evaluations
  ADD CONSTRAINT agent_evaluations_no_calls_ck
  CHECK (input_type <> 'call_transcript') NOT VALID;
ALTER TABLE agent_evaluations VALIDATE CONSTRAINT agent_evaluations_no_calls_ck;

ALTER TABLE interaction_analysis
  ADD CONSTRAINT interaction_analysis_no_calls_ck
  CHECK (input_type <> 'call_transcript') NOT VALID;
ALTER TABLE interaction_analysis VALIDATE CONSTRAINT interaction_analysis_no_calls_ck;

-- ---------------------------------------------------------------------------
-- 9 · Say what happened, out loud
--
-- A migration this destructive that prints nothing is one nobody can check
-- afterwards. Compare these counts against what scripts/dump_calls.py archived,
-- and run scripts/audit_data_integrity.py before trusting /report again.
-- ---------------------------------------------------------------------------

DO $report$
DECLARE
  n_int  bigint;
  n_eval bigint;
  n_chat bigint;
BEGIN
  SELECT count(*) INTO n_int  FROM interactions;
  SELECT count(*) INTO n_eval FROM agent_evaluations;
  SELECT count(*) INTO n_chat FROM interactions WHERE external_source = 'bitrix_chat_api';
  RAISE NOTICE '023 done. interactions=% (bitrix_chat_api=%), agent_evaluations=%',
               n_int, n_chat, n_eval;
  IF EXISTS (SELECT 1 FROM interactions WHERE external_source = 'asterisk_drive') THEN
    RAISE EXCEPTION '023 left call interactions behind';
  END IF;
END
$report$;

COMMIT;
