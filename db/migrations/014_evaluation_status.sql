-- 014 — evaluation status, and the one definition of a usable score.
--
-- WHAT WAS WRONG. Every consumer treated an `agent_evaluations` row as a
-- score. It is not. Pass 2 can return four different things and only one of
-- them is a number anybody may average:
--
--   ok              the rubric was applied and produced a score
--   contract_failed the response contradicted the rubric after the re-ask;
--                   the scoring engine refused to compute anything. No row is
--                   written for this today (workflow 02 routes it to
--                   `judge_failed` and retries) -- the value exists so that a
--                   row which somehow carries it can never be counted.
--   ungradeable     too little of the rubric survived evidence checking to
--                   average. Terminal DATA QUALITY, not a judge fault:
--                   retrying it manufactures a score and burns the budget.
--   unscoreable     there was nothing to evaluate -- the transcript held less
--                   speech than the scoring minimum. Also terminal, and it IS
--                   stored: workflow 02's `Store unscoreable outcome` writes
--                   one row with a null score before the job is dead-lettered,
--                   so the worst input-quality cases are counted rather than
--                   hidden from every report.
--
-- The old views made exactly the mistake this migration exists to stop:
-- `count(*)` counted every row as an evaluated interaction, while
-- `avg(final_score)` silently skipped the null ones. So a day on which the ASR
-- fell over read as "40 interactions evaluated, average 78" when it was
-- "12 scored, average 78, and 28 we could not grade". The denominator and the
-- numerator described different populations and nothing on the page said so.
--
-- WHAT THIS MIGRATION DOES.
--   1. Lifts status out of the raw_response JSON into first-class columns.
--   2. Defines "usable score" ONCE, as eval_score_is_usable(), and makes every
--      view call it rather than restate it.
--   3. Rebuilds v_agent_scorecard and v_quality_by_input so the counts and the
--      averages describe the same rows, grouped by the version co-ordinates
--      that make two means comparable at all.
--   4. Puts a COMPLETE confidence interval on every mean -- the observed
--      between-call spread, floored at the measured judge-repeat noise -- and
--      refuses to publish a performance band whose interval crosses a band
--      boundary. The noise measurement is itself versioned by the co-ordinate
--      it was measured on, so it can never be applied to a prompt, rubric or
--      model it never described.
--   5. Makes AMBER ASR SHADOW-ONLY (Sol's D1 rollout rule). A call evaluation
--      counts towards a published number only when its transcript came back
--      'green'; amber, red, unknown and missing are excluded from every mean,
--      interval, band and n_usable, and reported as amber_shadow_count instead.
--      The rows are still written and still stored -- shadow analysis reads
--      agent_evaluations -- they are simply never published. Defined once as
--      eval_asr_input_is_eligible(); see section 4a.
--
-- ATOMIC. The whole file runs inside one explicit transaction: it drops and
-- recreates two views, and a half-applied 014 leaves the reporting layer with
-- no scorecard at all. If the runner already opens a transaction per script
-- (n8n's Postgres node sends a multi-statement query as one implicit
-- transaction), the explicit BEGIN is harmless; if it does not (`psql -f`,
-- which autocommits statement by statement), the explicit BEGIN is the only
-- thing making this safe.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, guarded constraint adds, guarded
-- backfills, CREATE OR REPLACE FUNCTION, ON CONFLICT DO NOTHING seeds. The two
-- reporting views change COLUMN SHAPE, so they are dropped and recreated --
-- see section 5 for why that is a plain DROP and not a DROP ... CASCADE, and
-- for how their owner and grants survive it.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1 · status columns on agent_evaluations
-- ---------------------------------------------------------------------------
ALTER TABLE agent_evaluations
  ADD COLUMN IF NOT EXISTS contract_status     text NOT NULL DEFAULT 'ok',
  ADD COLUMN IF NOT EXISTS gradeable           boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS ungradeable_modules jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS evidence_rejected   jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS model_fingerprint   text;

-- Named, so the next migration can find it. Guarded on the name so a re-run
-- never re-validates the whole table.
DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'agent_evaluations_contract_status_check'
                    AND conrelid = 'agent_evaluations'::regclass) THEN
    ALTER TABLE agent_evaluations
      ADD CONSTRAINT agent_evaluations_contract_status_check
      CHECK (contract_status IN ('ok', 'contract_failed', 'ungradeable', 'unscoreable'));
  END IF;
END $do$;

COMMENT ON COLUMN agent_evaluations.contract_status IS
  'What pass 2 actually returned: ok | contract_failed | ungradeable | unscoreable. A score is usable ONLY when this is ok, gradeable is true and final_score is not null - see eval_score_is_usable().';
COMMENT ON COLUMN agent_evaluations.gradeable IS
  'Did enough of the rubric survive evidence checking to average? False means the weighted denominator fell below the scoring engine minimum, so final_score is null by construction and not by accident.';
COMMENT ON COLUMN agent_evaluations.ungradeable_modules IS
  'Modules struck out as evidence-ungroundable, with the reason. This is why a score can be null on a response that had no contract violation at all.';
COMMENT ON COLUMN agent_evaluations.evidence_rejected IS
  'Findings whose supporting quote did not match the transcript and were therefore discarded before scoring. A long list here is a prompt problem, not an agent problem.';
COMMENT ON COLUMN agent_evaluations.model_fingerprint IS
  'The provider system_fingerprint of the pass-2 call. A change of fingerprint is a change of baseline: means either side of it are NOT comparable, which is why it is a grouping key in v_agent_scorecard.';

-- ---------------------------------------------------------------------------
-- 2 · backfill
-- ---------------------------------------------------------------------------
-- Existing rows keep contract_status 'ok'. That is deliberate and it is the
-- conservative choice: we cannot retroactively tell an ungradeable row from an
-- unscoreable one -- the distinction did not exist when they were written --
-- and inventing it would put a measurement in the database that nobody made.
-- What we CAN say for certain is that a row with no final_score was never
-- graded, so gradeable goes false, and eval_score_is_usable() therefore
-- excludes it from every average whatever its status says.
--
-- These rows are visible in the views as `ok_without_score_count`: a bucket
-- that means "not graded, reason not recorded". It should stop growing the day
-- the new workflow ships; if it keeps growing, a writer is not sending status.
--
-- Naturally re-runnable: the second run matches nothing.
--
-- THE TIMESTAMP TRIGGER IS OFF FOR THIS ONE UPDATE, ON PURPOSE (round-4 review
-- finding). `t_eval_updated` (004_ai.sql) rewrites updated_at on every UPDATE,
-- so an unguarded backfill stamps migration day onto the whole null-score
-- history and destroys the only record of when each evaluation was last
-- actually written -- which is the column §6 of docs/PR2-db-status.md reads to
-- tell "pre-013 history" from "a writer that is still not sending status".
-- Nothing else in this transaction writes to agent_evaluations, and the trigger
-- is re-enabled immediately, so a failure anywhere below rolls the disable back
-- along with everything else.
--
-- ALTER TABLE ... DISABLE TRIGGER needs table ownership. So does the ADD COLUMN
-- in section 1, so this adds no new privilege requirement: a role that cannot
-- do it could not have reached this line.
ALTER TABLE agent_evaluations DISABLE TRIGGER t_eval_updated;

UPDATE agent_evaluations
   SET gradeable = false
 WHERE final_score IS NULL
   AND gradeable = true;

ALTER TABLE agent_evaluations ENABLE TRIGGER t_eval_updated;

-- ---------------------------------------------------------------------------
-- 3 · the index the status filters ride on
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_agent_evaluations_contract_status
    ON agent_evaluations (contract_status, gradeable);

-- ---------------------------------------------------------------------------
-- 4 · "usable score", defined once
-- ---------------------------------------------------------------------------
-- Every consumer that averages, counts or ranks a score MUST call this. The
-- rule restated by hand in four places is the rule that drifts in one of them,
-- and the failure is silent: a report that quietly includes ungradeable rows
-- looks exactly like a report that does not.
--
-- Deliberately NOT used in any index predicate. A functional index would
-- freeze this definition into stored index entries, and changing the function
-- would silently corrupt them.
CREATE OR REPLACE FUNCTION eval_score_is_usable(
  p_contract_status text,
  p_gradeable       boolean,
  p_final_score     numeric
) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
  SELECT p_contract_status = 'ok'
     AND coalesce(p_gradeable, false)
     AND p_final_score IS NOT NULL
$fn$;

COMMENT ON FUNCTION eval_score_is_usable(text, boolean, numeric) IS
  'THE definition of a usable score: contract_status = ok AND gradeable AND final_score IS NOT NULL. Do not restate this rule anywhere; call this.';

-- ---------------------------------------------------------------------------
-- 4a · "eligible input", defined once — the D1 rollout rule
-- ---------------------------------------------------------------------------
-- A usable score is not the same thing as a score we may PUBLISH. The judge can
-- apply the whole rubric to a transcript that is quietly wrong: an amber ASR
-- transcript returns text for every chunk, so nothing upstream refuses it, and
-- pass 2 scores the words it was given rather than the words that were spoken.
-- The result is a perfectly well-formed number about a conversation that did
-- not happen that way. `eval_score_is_usable()` cannot see any of this — it
-- only knows what pass 2 said about its own output.
--
-- Sol's D1 rollout rule, verbatim:
--
--     LEFT JOIN transcripts t ON t.interaction_id = e.interaction_id
--     WHERE ... AND (
--       e.input_type <> 'call_transcript'
--       OR t.asr_metrics->>'asr_quality_status' = 'green'
--     )
--
-- Chats stay eligible; a call evaluation counts only when its transcript's
-- asr_quality_status is exactly 'green'. Amber is SHADOW-ONLY: the row is still
-- written, still stored, still available to anyone comparing amber scores
-- against green ones — it just never reaches an average, an interval, a band or
-- an n_usable.
--
-- FAIL CLOSED, AND THE OPPOSITE OF 013. 013's evaluate_alert_rules() reads
-- `coalesce(t.asr_metrics->>'asr_quality_status', 'green')`: an alert about a
-- call whose quality nobody recorded is still worth a human's attention, so
-- unknown defaults to the permissive value there. A SCORECARD is the other
-- direction. A missing transcript row, a transcript with no asr_metrics, or a
-- status this rule has never heard of must all be EXCLUDED, because publishing
-- a grade about somebody's work on evidence we cannot vouch for is worse than
-- publishing nothing. There is deliberately no coalesce to 'green' here: with a
-- LEFT JOIN, all three of those cases make the comparison NULL, and the
-- coalesce below turns NULL into false rather than into 'green'.
--
-- ONE TRANSCRIPT PER CALL, so "the current transcript" needs no tie-break:
-- transcripts.interaction_id is `NOT NULL UNIQUE` (003_interactions.sql), and
-- 012's leases exist precisely to stop two writers racing for that one row. The
-- LEFT JOIN below can therefore match at most one row and cannot fan the
-- evaluation out. If a later migration ever relaxes that uniqueness, every join
-- added by this migration must become a LATERAL picking the newest
-- transcribed_at, and the acceptance script's checks 6c/6d are what will catch it.
--
-- A FUNCTION, not the predicate copy-pasted into three views, for exactly the
-- reason eval_score_is_usable() is a function: the rule restated by hand in
-- three places is the rule that drifts in one of them, silently. The JOIN stays
-- written out in each view because a function cannot carry a join; only the
-- predicate is centralised.
--
-- p_input_type is the `input_type` ENUM (001_extensions_and_enums.sql:
-- 'chat' | 'call_transcript'), which is the declared type of both
-- agent_evaluations.input_type and interaction_analysis.input_type.
CREATE OR REPLACE FUNCTION eval_asr_input_is_eligible(
  p_input_type         input_type,
  p_asr_quality_status text
) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
  SELECT coalesce(
           p_input_type <> 'call_transcript'
        OR p_asr_quality_status = 'green',
           false)
$fn$;

COMMENT ON FUNCTION eval_asr_input_is_eligible(input_type, text) IS
  'THE definition of an input whose score may be published (Sol''s D1 rule): chats always; call transcripts only when asr_metrics->>''asr_quality_status'' is exactly ''green''. Amber, red, an unknown status, a transcript without asr_metrics and a missing transcript row are ALL excluded - fail closed, which is the OPPOSITE of the coalesce-to-green default 013 uses for alerting. Do not restate this rule anywhere; call this.';

-- The same rule in row form, for consumers that want the population rather
-- than the predicate. Anything that reports "how many calls did we score"
-- should count rows here, and nothing should count rows in agent_evaluations.
--
-- THE D1 RULE APPLIES HERE TOO. This view is the row form of "a score anybody
-- may average", and after the D1 rule an amber call's score is not one. Leaving
-- it out would have left two populations both calling themselves "usable" and
-- disagreeing by exactly the rows this migration exists to exclude. Amber rows
-- remain in `agent_evaluations` for shadow analysis; query that table directly
-- (with the same join) when comparing amber against green.
--
-- The column list is unchanged -- `e.*` still expands to the agent_evaluations
-- columns and nothing else -- so this stays a CREATE OR REPLACE.
CREATE OR REPLACE VIEW v_usable_evaluations AS
SELECT e.*
FROM agent_evaluations e
LEFT JOIN transcripts t ON t.interaction_id = e.interaction_id
WHERE eval_score_is_usable(e.contract_status, e.gradeable, e.final_score)
  AND eval_asr_input_is_eligible(e.input_type,
                                 t.asr_metrics->>'asr_quality_status');

COMMENT ON VIEW v_usable_evaluations IS
  'agent_evaluations restricted to rows carrying a score anybody may average AND publish: eval_score_is_usable() AND eval_asr_input_is_eligible(). Amber/unknown-ASR call evaluations are stored but never appear here - read agent_evaluations directly for shadow analysis. A view expands * at creation time, so re-run 014 after any migration that adds a column to agent_evaluations.';

-- The performance bands, mirroring services/worker/app/evaluate/scoring.py
-- performance_level(). Two copies of a boundary is one copy too many, but SQL
-- cannot import Python; acceptance query R4 in docs/PR2-db-status.md pins them
-- against each other. Boundaries: 85 / 70 / 55.
CREATE OR REPLACE FUNCTION eval_performance_band(p_score numeric)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
  SELECT CASE
           WHEN p_score IS NULL THEN NULL
           WHEN p_score >= 85   THEN 'Excellent'
           WHEN p_score >= 70   THEN 'Good'
           WHEN p_score >= 55   THEN 'Average'
           ELSE                      'Below Average'
         END
$fn$;

-- ---------------------------------------------------------------------------
-- 4b · the uncertainty parameters, as data and not as folklore
-- ---------------------------------------------------------------------------
-- TWO TABLES, BECAUSE THERE ARE TWO KINDS OF CONSTANT HERE.
--
--   eval_report_params   co-ordinate-free reporting policy: the normal
--                        quantile, and the minimum N at which a mean may be
--                        published at all. Neither depends on which prompt or
--                        model produced the scores.
--   eval_noise_params    a MEASUREMENT -- and a measurement belongs to the
--                        thing it was measured on.
--
-- WHY THE NOISE MEASUREMENT IS VERSIONED (round-4 review finding). The first
-- draft held one global `repeat_run_variance` row while the views grouped
-- means by (prompt_version, rubric_version, model, model_fingerprint).
-- Re-measuring the noise after a prompt change -- which is exactly when you
-- must re-measure it -- would then have retroactively re-stated the
-- uncertainty of every historical version group with a number that was never
-- measured on it. The variance is now keyed by the same four co-ordinates the
-- views group by, and each view row looks it up on its OWN co-ordinate.
--
-- NO MATCH IS NOT ZERO. The lookup returns NULL, both half-widths return NULL,
-- and band_stable reads false. An unmeasured co-ordinate publishes nothing.
--
-- model_fingerprint NULL means WILDCARD -- "measured before fingerprints were
-- captured, or not specific to one". An exact-fingerprint row wins over a
-- wildcard row for the same prompt/rubric/model.
--
-- WHERE 188.70 COMES FROM, AND WHAT IT DESCRIBES. Day-13 A/A run: the SAME
-- prompts, the same code and the same 81 calls, judged twice. Scores moved by
-- MAE 6.81 / RMSE 13.95 and 11 of 68 performance bands flipped with no prompt
-- change at all. The variance of those repeat-run differences is 188.70
-- (docs/PR2-judge-integrity.md, "What iteration 2 measured").
--
-- It was measured on ONE co-ordinate, and it is bound to that co-ordinate here:
--   prompt_version    'pass2-agent-quality-v3'   -- the A/A ran the OLD pass-2
--                     prompt on BOTH sides (pass 1 was pass1-customer-v4);
--                     agent_evaluations.prompt_version stores the pass-2 label
--   rubric_version    '1.0.0'
--   model             'deepseek-chat'            -- the legacy alias in use
--                     that day; see docs/PR2-judge-integrity.md finding 3
--   model_fingerprint NULL                       -- none was captured then
--
-- READ THIS BEFORE ASKING WHY NOTHING IS PUBLISHABLE AFTER ROLLOUT. The worker
-- ships whatever judge.PASS2_VERSION currently is -- pass2-agent-quality-v6 as
-- this migration is written, and it has moved twice since the A/A run -- against
-- deepseek-v4-flash. That co-ordinate has NO measured noise floor, so every
-- scorecard row will read noise_variance NULL, ci95_half_width NULL and
-- band_stable false until the A/A run is repeated on the shipping prompt and
-- model and its variance INSERTed here. That is intended, and it is the honest
-- behaviour: the alternative is reusing a several-prompt-generations-old
-- measurement as if it described the judge that is actually running.
--
-- HONEST ABOUT THE STATISTICS. 188.70 is the variance of the DIFFERENCE between
-- two runs, which for independent runs is about twice the variance of one run.
-- Used directly as a per-call variance it therefore makes the noise floor
-- roughly sqrt(2) WIDER than a single run's judge noise. That is the direction
-- to be wrong in. It remains a FLOOR on the uncertainty and never the whole of
-- it -- it says nothing about which calls an agent happened to take -- which is
-- why section 5 takes the LARGER of it and the observed between-call variance
-- rather than using it alone.
--
-- TABLES, not literals, for the same reason alert_rules holds thresholds:
-- re-measuring is an INSERT, not a deploy. Seeded ON CONFLICT DO NOTHING so a
-- re-run never overwrites a value somebody re-measured.

CREATE TABLE IF NOT EXISTS eval_report_params (
  param_key    text PRIMARY KEY,
  value        numeric NOT NULL,
  source       text NOT NULL,
  notes        text,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE eval_report_params IS
  'Co-ordinate-free reporting policy read by the CI functions and the reporting views: z_95 and min_n_publish. Nothing here depends on which prompt or model produced a score; anything that does belongs in eval_noise_params.';

INSERT INTO eval_report_params (param_key, value, source, notes) VALUES

  ('z_95', 1.959964,
   'standard normal, two-sided 95%',
   'Normal, not t. With the N>=30 publication floor below the difference is under 4%, and the variance estimate is far more uncertain than the quantile.'),

  ('min_n_publish', 30,
   'reviewer decision (Sol, section 1): continuous agent means over at least 30 scoreable calls',
   'Minimum usable scores before a mean is published at all. At N=30 the judge-noise floor ALONE is +/-4.9 points, already a third of a band; below that a mean is not a measurement.')

ON CONFLICT (param_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS eval_noise_params (
  param_key         text    NOT NULL,
  prompt_version    text    NOT NULL,
  rubric_version    text    NOT NULL,
  model             text    NOT NULL,
  -- NULL = wildcard: this measurement is not specific to one fingerprint.
  model_fingerprint text,
  value             numeric NOT NULL,
  measured_on       date,
  source            text    NOT NULL,
  notes             text,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- coalesce(), not a plain unique index over the five columns: NULLs are
-- DISTINCT to a unique index, so without it two wildcard rows for the same
-- co-ordinate could both exist and the lookup below would silently pick one.
CREATE UNIQUE INDEX IF NOT EXISTS eval_noise_params_coordinate
    ON eval_noise_params (param_key, prompt_version, rubric_version, model,
                          coalesce(model_fingerprint, ''));

COMMENT ON TABLE eval_noise_params IS
  'Measured judge-repeat variance, keyed by the version co-ordinate it was measured on (prompt_version, rubric_version, model, model_fingerprint; NULL fingerprint = wildcard). The reporting views look it up on their own grouping co-ordinate and treat "no row" as NULL, which makes band_stable false. Re-measure after any prompt, rubric or model change and INSERT a NEW row -- never edit an old one to mean a new co-ordinate.';

INSERT INTO eval_noise_params (param_key, prompt_version, rubric_version, model,
                               model_fingerprint, value, measured_on, source, notes) VALUES

  ('repeat_run_variance', 'pass2-agent-quality-v3', '1.0.0', 'deepseek-chat', NULL,
   188.70, DATE '2026-08-13',
   'day-13 A/A run: old prompts (pass1_customer_v4 / pass2_agent_quality_v3) through new code, 81 calls, two runs',
   'Variance of the per-call repeat-run difference. MAE 6.81, RMSE 13.95, 11/68 band flips with no prompt change at all. About twice a single run''s judge variance, so it is a conservative FLOOR. Bound to the co-ordinate it was measured on: it says nothing about pass2-agent-quality-v4 or v5, and nothing about deepseek-v4-flash.')

ON CONFLICT (param_key, prompt_version, rubric_version, model,
             coalesce(model_fingerprint, '')) DO NOTHING;

-- Drafts of this migration defined eval_noise_param(text) and
-- eval_ci_half_width_95(bigint). CREATE OR REPLACE cannot replace a function
-- with a DIFFERENT signature -- it adds an overload -- and a stale overload is
-- a second definition of the rule this file exists to define once.
DROP FUNCTION IF EXISTS eval_noise_param(text);
DROP FUNCTION IF EXISTS eval_ci_half_width_95(bigint);

-- SECURITY DEFINER, AND NOT AN ACCIDENT (round-5 review finding F4). A view
-- runs its own body with the VIEW OWNER's rights, so a reporting role holding
-- only SELECT on v_agent_scorecard can read agent_evaluations through it. A
-- FUNCTION called from that view does NOT inherit that: a SECURITY INVOKER
-- function runs as the CALLER, so the moment the view body calls this one the
-- reporter needs SELECT on eval_report_params in their own right -- and the
-- whole scorecard errors with "permission denied for table eval_report_params"
-- for a role that was deliberately given nothing but three views.
--
-- Two ways out. GRANT SELECT ON eval_report_params TO PUBLIC would work and
-- would keep the table non-writable, but it publishes the reporting policy
-- table to every role in the cluster and has to be re-granted by hand after
-- any future rebuild of the table. SECURITY DEFINER keeps the tables reachable
-- through exactly one, read-only, parameterised entry point and leaves them
-- invisible otherwise, which is the stricter of the two. That is the one taken.
--
-- SET search_path = pg_catalog, public is MANDATORY on a definer function: a
-- caller who can create a schema could otherwise put their own
-- `eval_report_params` in front of ours and have it read with the definer's
-- rights. Pinned here, and the table is schema-qualified as well, so neither
-- the search_path nor the object name is under the caller's control. pg_temp
-- is deliberately absent from the list.
--
-- THE BODY IS A CONSTANT STRING. No dynamic SQL, no EXECUTE, no format(): the
-- only caller-supplied value is a parameter used as a value. There is nothing
-- here for a definer's rights to be turned into.
--
-- COSTS, stated so nobody discovers them later: (1) a function carrying
-- SECURITY DEFINER or a SET clause is NEVER inlined by the planner, so this
-- becomes a real call per row instead of folding into the query. Both callers
-- are per-GROUP scalars in a LATERAL, not per-row predicates, so the count is
-- the number of scorecard rows. (2) EXECUTE on a new function is granted to
-- PUBLIC by default and that is kept ON PURPOSE -- revoking it would break the
-- very reporter this fix exists for. The function reads two rows of policy
-- constants and writes nothing.
CREATE OR REPLACE FUNCTION eval_report_param(p_key text)
RETURNS numeric
LANGUAGE sql STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fn$
  SELECT p.value FROM public.eval_report_params p WHERE p.param_key = p_key
$fn$;

COMMENT ON FUNCTION eval_report_param(text) IS
  'A co-ordinate-free reporting constant (z_95, min_n_publish). NULL when the row is missing, and every gate that reads it coalesces that to false. SECURITY DEFINER with search_path pinned to pg_catalog, public: a view-only reporting role must be able to read the scorecard without holding SELECT on eval_report_params, and the table stays unreadable and unwritable except through this parameterised lookup.';

-- The noise lookup MUST be called with the same four values the caller grouped
-- by. An exact fingerprint match beats the wildcard row; no match is NULL.
--
-- SECURITY DEFINER for the same reason as eval_report_param() above (F4), with
-- the same pinned search_path and the same schema-qualified table reference.
-- eval_noise_params in particular must stay unreadable AND unwritable by a
-- reporting role: it is the measurement that decides what may be published, so
-- a role that could edit it could publish anything it liked.
CREATE OR REPLACE FUNCTION eval_noise_param(
  p_key               text,
  p_prompt_version    text,
  p_rubric_version    text,
  p_model             text,
  p_model_fingerprint text
) RETURNS numeric
LANGUAGE sql STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fn$
  SELECT p.value
    FROM public.eval_noise_params p
   WHERE p.param_key      = p_key
     AND p.prompt_version = p_prompt_version
     AND p.rubric_version = p_rubric_version
     AND p.model          = p_model
     AND (p.model_fingerprint = p_model_fingerprint
          OR p.model_fingerprint IS NULL)
   ORDER BY (p.model_fingerprint IS NULL)
   LIMIT 1
$fn$;

COMMENT ON FUNCTION eval_noise_param(text, text, text, text, text) IS
  'The measured noise parameter for ONE version co-ordinate. An exact fingerprint match wins over the NULL-fingerprint wildcard row. Returns NULL when nothing was measured on this co-ordinate, which makes both half-widths NULL and band_stable false. SECURITY DEFINER with search_path pinned to pg_catalog, public, so a view-only reporting role can read the scorecard while eval_noise_params itself stays unreadable and unwritable to it.';

-- The judge-noise FLOOR alone: what a mean would move by if the only source of
-- movement were asking the same judge the same question twice.
CREATE OR REPLACE FUNCTION eval_noise_floor_half_width_95(
  p_n         bigint,
  p_noise_var numeric
) RETURNS numeric
LANGUAGE sql STABLE AS $fn$
  SELECT CASE
           WHEN p_n IS NULL OR p_n < 1 OR p_noise_var IS NULL THEN NULL
           -- schema-qualified: this function is SECURITY INVOKER and runs
           -- under the CALLER's search_path, so an unqualified name here could
           -- be resolved to a caller-owned shadow of eval_report_param().
           ELSE round(public.eval_report_param('z_95')
                        * sqrt(p_noise_var / p_n::numeric), 2)
         END
$fn$;

COMMENT ON FUNCTION eval_noise_floor_half_width_95(bigint, numeric) IS
  'The judge-repeat noise floor on a mean of N usable scores. A LOWER BOUND on the uncertainty, published as its own column so the gap between it and ci95_half_width is visible. It is NOT the publication gate -- eval_ci_half_width_95() is.';

-- THE COMPLETE INTERVAL (round-4 review finding). The first draft published the
-- noise floor above AS IF it were the confidence interval, and gated
-- band_stable on it. It is not one: it omits the between-call sampling variance
-- -- which calls an agent happened to take that month -- and a LOWER BOUND on
-- the uncertainty cannot prove a band is stable. The standard error is
--
--     se = sqrt( max(sample_var, noise_var) / N )
--
-- where sample_var is that group's observed var_samp() over its usable scores.
--
-- max(), not a sum. The two are not independent contributions to be added: the
-- judge's run-to-run noise is ALREADY inside the observed spread of real
-- scores, so adding them would count it twice. Taking the larger keeps the
-- floor for a group whose scores happen to sit close together by luck, and lets
-- the real spread dominate whenever it exceeds the floor -- which on live data
-- it usually will, because agents differ call to call by more than the judge
-- differs run to run.
--
-- sample_var is NULL at N < 2 (var_samp of one row); coalesce to 0 lets the
-- floor stand alone there, and the N >= 30 gate makes that case unpublishable
-- regardless.
--
-- p_noise_var NULL -> NULL -> band_stable false. Fail closed: a co-ordinate
-- whose noise floor was never measured publishes nothing.
CREATE OR REPLACE FUNCTION eval_ci_half_width_95(
  p_n          bigint,
  p_sample_var numeric,
  p_noise_var  numeric
) RETURNS numeric
LANGUAGE sql STABLE AS $fn$
  SELECT CASE
           WHEN p_n IS NULL OR p_n < 1 OR p_noise_var IS NULL THEN NULL
           -- schema-qualified, same reason as eval_noise_floor_half_width_95.
           ELSE round(public.eval_report_param('z_95')
                        * sqrt(greatest(coalesce(p_sample_var, 0::numeric),
                                        p_noise_var) / p_n::numeric), 2)
         END
$fn$;

COMMENT ON FUNCTION eval_ci_half_width_95(bigint, numeric, numeric) IS
  '95% half-width of a mean over N usable scores, built from the LARGER of the observed sample variance and the measured judge-noise variance for that version co-ordinate. THIS is the publication gate. NULL when N < 1 or the co-ordinate has no measured noise floor, and every band gate treats NULL as not publishable.';

-- ---------------------------------------------------------------------------
-- 5 · the reporting views
-- ---------------------------------------------------------------------------
-- DROP, not CREATE OR REPLACE: both views change column shape (new grouping
-- keys, new counts), and CREATE OR REPLACE can only append columns.
--
-- Plain DROP, never DROP ... CASCADE. If something downstream depends on one
-- of these views the DROP must FAIL and be looked at -- a cascade would
-- silently delete a report somebody built. The runbook preflight P1 in
-- scripts/sql/acceptance_014_status.sql lists the query that finds those
-- dependants first.
--
-- DROP + CREATE MAKES NEW OBJECTS, AND NEW OBJECTS HAVE NO GRANTS (round-4
-- review finding). CREATE OR REPLACE would have preserved owner and ACL; DROP
-- does not, so a reporting role that could SELECT these views this morning
-- silently loses that privilege this afternoon and finds out from a broken
-- dashboard. Owner and full ACL are therefore snapshotted into a temp table
-- BEFORE the DROP and replayed after the CREATE, inside this same transaction.
-- Preflight P3 prints the same information for the human record, because a
-- temp table is not a record.
DROP TABLE IF EXISTS pg_temp.v013_view_acls;
CREATE TEMP TABLE v013_view_acls AS
SELECT c.relname                   AS view_name,
       pg_get_userbyid(c.relowner) AS view_owner,
       c.relacl                    AS view_acl
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'v'
   AND n.nspname = current_schema()
   AND c.relname IN ('v_agent_scorecard', 'v_quality_by_input');

DROP VIEW IF EXISTS v_agent_scorecard;
DROP VIEW IF EXISTS v_quality_by_input;

-- ---------------------------------------------------------------------------
-- v_agent_scorecard
--
-- GROUPED BY VERSION. prompt_version, rubric_version, model and
-- model_fingerprint are grouping keys, not decoration. A mean that mixes two
-- prompt versions is a mean of two different measurements: the day-13 data put
-- the prompt-attributable RMS at 14.15 points, which is a whole band. One
-- agent therefore gets one ROW PER VERSION CO-ORDINATE, and a version change
-- shows up as a new row starting at N=1 rather than as a mysterious drift in
-- an old one. Never compare rows whose version columns differ.
--
-- Those four columns are also exactly what the noise lookup is keyed on, so the
-- interval on a group can only ever be built from a measurement taken on that
-- group's own co-ordinate.
--
-- COUNTS AND AVERAGES NOW DESCRIBE THE SAME ROWS. Every average is taken over
-- usable rows only, and the five status counts partition
-- evaluated_interactions exactly:
--
--   scored_interactions + ungradeable_count + unscoreable_count
--     + contract_failed_count + ok_without_score_count = evaluated_interactions
--
-- Acceptance query R1 in docs/PR2-db-status.md asserts that.
--
-- AMBER ASR IS SHADOW-ONLY (Sol's D1 rollout rule). Every row this view
-- averages, counts or bands is a chat, or a call whose transcript came back
-- 'green'. A call evaluation whose transcript is amber, red, statusless or
-- missing is EXCLUDED here and counted in amber_shadow_count instead: the row
-- stays in agent_evaluations for shadow analysis, and never reaches a mean, an
-- interval, a band or n_usable. The predicate is eval_asr_input_is_eligible()
-- and the reasoning is in section 4a.
--
-- THE GROUP UNIVERSE IS TAKEN BEFORE THAT FILTER. If a version co-ordinate's
-- every row was shadowed -- an agent who spent a month on a bad line, which is
-- exactly the case the rule is for -- the group still appears, reading
-- evaluated_interactions 0 and amber_shadow_count N. Filtering the rows away
-- and then grouping would have deleted that agent from the scorecard silently,
-- and "silently" is the whole complaint this migration answers.
--
-- Written as an aggregate CTE with the derived columns computed once on top of
-- it. The first draft repeated `count(*) FILTER (WHERE u.usable)` eight times
-- in one SELECT list; that is the shape in which one of the eight eventually
-- gets a different filter and nobody notices.
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
  -- Sol's D1 join, verbatim. At most one row can match: transcripts.
  -- interaction_id is NOT NULL UNIQUE (003_interactions.sql), so there is no
  -- "current transcript" tie-break to get wrong and no risk of fanning an
  -- evaluation out into several rows. A LEFT join, not an inner one, because a
  -- CHAT evaluation has no transcript and must still be counted.
  LEFT JOIN transcripts t ON t.interaction_id = e.interaction_id
  CROSS JOIN LATERAL (
    SELECT eval_asr_input_is_eligible(e.input_type,
                                      t.asr_metrics->>'asr_quality_status')
             AS asr_eligible
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
    count(*) FILTER (WHERE src.channel =  'phone_call') AS calls,
    count(*) FILTER (WHERE src.channel <> 'phone_call') AS chats,

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
  -- amber, red, an unrecognised status, no asr_metrics, or no transcript row at
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

COMMENT ON VIEW v_agent_scorecard IS
  'One row per agent PER VERSION CO-ORDINATE (prompt_version, rubric_version, model, model_fingerprint). Covers chats and GREEN-ASR calls only (Sol''s D1 rule, eval_asr_input_is_eligible): amber/red/unknown-ASR call evaluations are excluded from every count and average here and reported in amber_shadow_count, while the rows stay in agent_evaluations for shadow analysis. Averages cover usable rows only; the five status counts partition evaluated_interactions, and amber_shadow_count is OUTSIDE that partition. A group whose every row was shadowed still appears, reading evaluated_interactions 0. ci95_half_width is the COMPLETE interval (observed spread, floored at the judge noise measured on THIS co-ordinate); noise_floor_half_width is the floor alone, shown for comparison. Publish a band only where band_stable is true. Never compare rows whose version columns differ.';

-- ---------------------------------------------------------------------------
-- v_quality_by_input
--
-- Does the model score calls worse than chats, or is the transcript just bad?
-- This is the first thing to look at when QA numbers look odd -- and the first
-- place the old counting bug bit, because a bad ASR day filled the row with
-- ungradeable evaluations and then reported the average of the handful that
-- survived as if it were the average of all of them. Now `n` (rows seen) and
-- `n_usable` (rows averaged) sit next to each other and the gap is the story.
--
-- SOL'S D1 RULE APPLIES HERE TOO, AND IT COSTS THIS VIEW SOMETHING. This view
-- publishes means and band_stable, so it must not publish an amber call's
-- score; but it is also the view you open to find out how bad the ASR is, and
-- filtering the bad calls out of `n` would have quietly deleted the worst
-- evidence from the diagnostic. Both are handled: `n` counts only what the rule
-- lets through, and `amber_shadow_count` counts what it removed, on the SAME
-- (input_type, diarization, confidence_bucket) row -- the shadowed rows keep
-- their transcript's diarization and confidence bucket, because those come from
-- the transcript that was rejected. So the bad-ASR story is still readable
-- here, it has just moved one column to the right.
--
-- A row whose entire population was shadowed still appears (group universe
-- taken before the filter), reading n 0 / amber_shadow_count N. On a bad ASR
-- day that is the row you want to see, and the previous shape would have
-- deleted it.
--
-- NOTE THE POPULATION DIFFERENCE. This view has no join to agents, so unlike
-- v_agent_scorecard it DOES include rows with a null agent_id and rows against
-- bot conversations. That is deliberate -- an ASR problem is a problem whoever
-- handled the call -- and it is why the two views' totals need not match.
-- ---------------------------------------------------------------------------
CREATE VIEW v_quality_by_input AS
-- Same three-part shape as v_agent_scorecard, and for the same reasons: one
-- join block, a group universe taken before the eligibility filter, aggregates
-- over eligible rows only, and the excluded rows counted beside them.
WITH src AS (
  SELECT
    e.input_type,
    t.diarization,
    width_bucket(coalesce(t.asr_confidence, 1.0), 0, 1, 5) AS confidence_bucket,

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
  -- One transcript per interaction (transcripts.interaction_id is NOT NULL
  -- UNIQUE, 003_interactions.sql), so this join cannot fan a row out and there
  -- is no "current transcript" to choose between.
  LEFT JOIN transcripts t ON t.interaction_id = e.interaction_id
  CROSS JOIN LATERAL (
    SELECT eval_asr_input_is_eligible(e.input_type,
                                      t.asr_metrics->>'asr_quality_status')
             AS asr_eligible
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
-- be NULL on a row with no transcript or no captured fingerprint, so those
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

COMMENT ON VIEW v_quality_by_input IS
  'Score quality by input type and ASR confidence, grouped by version co-ordinates. Read THREE columns, not two: `n` is rows seen after Sol''s D1 rule, `n_usable` is rows averaged, and `amber_shadow_count` is the call evaluations the D1 rule removed from `n` altogether (amber/red/unknown ASR). A large n-to-n_usable gap is a transcription problem, not an agent problem; a large amber_shadow_count is the same problem one stage earlier. Unlike v_agent_scorecard this view does NOT exclude null-agent or bot rows.';

-- ---------------------------------------------------------------------------
-- 5b · put the owner and the grants back
-- ---------------------------------------------------------------------------
-- The two views above are NEW objects. Without this they are owned by whoever
-- ran the migration and readable by nobody else. This replays exactly what was
-- there, including WITH GRANT OPTION and PUBLIC grants. A database where the
-- views carried no explicit ACL (relacl NULL -- owner-only defaults) replays
-- nothing, which is also correct.
--
-- If the migration role does not own the views, ALTER VIEW ... OWNER TO raises
-- and the whole transaction rolls back. That is the loud failure we want, and
-- preflight P3 is how you find out before starting rather than during.
--
-- REPLAY IS A RESTORE, NOT AN ADDITION (round-5 review finding F5). The first
-- draft only ever ran GRANTs. That is not the same thing as putting the ACL
-- back, for two reasons:
--
--   1. A NEW OBJECT IS NOT NECESSARILY A BLANK ONE. `ALTER DEFAULT PRIVILEGES
--      ... IN SCHEMA public GRANT SELECT ON TABLES TO reporter` stamps its
--      grants onto every relation the creating role makes from then on --
--      including the two views this migration recreates. Those grants are
--      INHERITED, not restored, and a GRANT-only replay leaves them in place.
--      The migration then reports "the ACL survived" while the object carries
--      privileges that were never in the snapshot: exactly the silent widening
--      this section exists to prevent.
--
--   2. PUBLIC. Same mechanism, worse blast radius: a default grant to PUBLIC
--      makes the scorecard world-readable and a GRANT-only replay never
--      notices.
--
-- So the order is: OWNER, then CLEAR, then REPLAY, then ASSERT.
--
-- The clear step revokes from EVERY non-owner grantee found on the recreated
-- object -- not merely from the ones absent from the snapshot. Revoking a
-- grantee that IS in the snapshot costs nothing (the replay immediately puts
-- it back, in exactly the privileges the snapshot recorded) and it removes a
-- whole class of near-miss: an inherited default granting a snapshotted
-- grantee MORE than the snapshot recorded -- INSERT on top of a snapshotted
-- SELECT, or a WITH GRANT OPTION the snapshot did not have -- which a
-- difference-only revoke would keep.
--
-- The OWNER is deliberately never revoked from. Its entry is an artefact of
-- ownership rather than of a GRANT, and stripping it would leave the migration
-- role unable to read the view it just made.
--
-- WHEN relacl IS NULL THERE IS NOTHING TO CLEAR, and no REVOKE is issued. That
-- case matters: relacl IS NULL means "never granted, owner-only defaults", and
-- it is not reachable again once any GRANT or REVOKE has touched the object.
-- A pointless REVOKE would materialise a {owner=arwdDxt/owner} ACL that is
-- semantically identical but no longer prints as "(no explicit ACL)" -- and
-- acceptance check 7 compares exactly that text against preflight P3.
--
-- GRANTOR. An aclitem records who granted it, and a GRANT run by the migration
-- role records the migration role. Where the original grantor is somebody else
-- this block SET LOCAL ROLEs to them and grants as them, so the grantor is
-- preserved exactly; the snapshot owner's own grants are replayed FIRST so a
-- delegated grantor already holds its grant option by the time its own grants
-- are replayed. Where that is not possible -- the migration role is not a
-- member of the grantor, or the grantor no longer holds the grant option on
-- the recreated object -- it FALLS BACK to granting as the migration role and
-- RAISEs a NOTICE naming the grant. The privilege is then correct and the
-- grantor column is the migration role: that is the documented, deliberate
-- residue of a DROP/CREATE, and it is what the assertion below means by
-- "modulo grantor". Nothing is skipped silently.
--
-- THE ASSERTION IS THE POINT. After the replay each view's live ACL is
-- exploded, the owner's own entry dropped, and the resulting
-- (grantee, privilege_type, is_grantable) multiset compared BOTH WAYS against
-- the same projection of the snapshot. Anything missing is a privilege the
-- DROP destroyed; anything extra is a privilege the CREATE invented. Either
-- RAISEs, and the RAISE rolls back the whole migration -- there is no state in
-- which 014 commits carrying an ACL it cannot account for.
DO $do$
DECLARE
  v_schema    text := current_schema();
  v           record;
  g           record;
  r           record;
  grant_stmt  text;
  as_grantor  boolean;
  n_missing   int;
  n_extra     int;
  s_missing   text;
  s_extra     text;
  n_regrantor int := 0;
BEGIN
  FOR v IN SELECT * FROM v013_view_acls LOOP

    -- 1 . owner first, so the owner's implicit ACL entry is already correct
    --     before anything is compared against it.
    EXECUTE format('ALTER VIEW %I.%I OWNER TO %I',
                   v_schema, v.view_name, v.view_owner);

    -- 2 . clear every non-owner privilege the CREATE inherited.
    FOR r IN
      SELECT DISTINCT
             CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                  ELSE quote_ident(pg_get_userbyid(a.grantee)) END AS grantee_sql
        FROM pg_class c
        JOIN pg_namespace ns ON ns.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) a
       WHERE ns.nspname   = v_schema
         AND c.relname    = v.view_name
         AND c.relacl IS NOT NULL
         AND a.grantee   <> c.relowner
    LOOP
      EXECUTE format('REVOKE ALL ON %I.%I FROM %s',
                     v_schema, v.view_name, r.grantee_sql);
    END LOOP;

    -- 3 . replay the snapshot exactly, preserving grantor where possible.
    IF v.view_acl IS NOT NULL THEN
      FOR g IN
        SELECT a.privilege_type,
               a.is_grantable,
               CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                    ELSE quote_ident(pg_get_userbyid(a.grantee)) END AS grantee_sql,
               pg_get_userbyid(a.grantor) AS grantor_name
          FROM aclexplode(v.view_acl) a
         -- the owner's own grants first: a delegated grantor must already hold
         -- its WITH GRANT OPTION before we try to grant AS it.
         ORDER BY (pg_get_userbyid(a.grantor) IS DISTINCT FROM v.view_owner),
                  a.grantee, a.privilege_type
      LOOP
        grant_stmt := format('GRANT %s ON %I.%I TO %s%s',
                             g.privilege_type,
                             v_schema, v.view_name,
                             g.grantee_sql,
                             CASE WHEN g.is_grantable THEN ' WITH GRANT OPTION'
                                  ELSE '' END);
        as_grantor := false;

        IF g.grantor_name IS DISTINCT FROM current_user THEN
          BEGIN
            EXECUTE format('SET LOCAL ROLE %I', g.grantor_name);
            EXECUTE grant_stmt;
            as_grantor := true;
          EXCEPTION WHEN OTHERS THEN
            -- the subtransaction rollback also undoes the SET LOCAL ROLE
            as_grantor := false;
          END;
          RESET ROLE;
        END IF;

        IF NOT as_grantor THEN
          EXECUTE grant_stmt;
          IF g.grantor_name IS DISTINCT FROM current_user THEN
            n_regrantor := n_regrantor + 1;
            RAISE NOTICE
              '014 ACL replay: % on %.% to % was originally granted by %; re-granted by % instead (grantor not assumable). Privilege preserved, grantor changed.',
              g.privilege_type, v_schema, v.view_name, g.grantee_sql,
              g.grantor_name, current_user;
          END IF;
        END IF;
      END LOOP;
    END IF;

    -- 4 . assert the result equals the snapshot, modulo grantor.
    --     No coalesce to an empty aclitem[] anywhere below: aclexplode()
    --     rejects a zero-dimensional array ("ACL arrays must be
    --     one-dimensional") but is STRICT, so a NULL relacl already yields
    --     zero rows -- which is precisely the "owner-only defaults" case.
    WITH want AS (
      SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                  ELSE pg_get_userbyid(a.grantee) END AS grantee_name,
             a.privilege_type,
             a.is_grantable
        FROM aclexplode(v.view_acl) a
       WHERE a.grantee <> (SELECT c.relowner
                             FROM pg_class c
                             JOIN pg_namespace ns ON ns.oid = c.relnamespace
                            WHERE ns.nspname = v_schema
                              AND c.relname  = v.view_name)
    ), got AS (
      SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                  ELSE pg_get_userbyid(a.grantee) END AS grantee_name,
             a.privilege_type,
             a.is_grantable
        FROM pg_class c
        JOIN pg_namespace ns ON ns.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) a
       WHERE ns.nspname = v_schema
         AND c.relname  = v.view_name
         AND a.grantee <> c.relowner
    ), miss AS (
      SELECT * FROM want EXCEPT ALL SELECT * FROM got
    ), extra AS (
      SELECT * FROM got EXCEPT ALL SELECT * FROM want
    )
    SELECT (SELECT count(*) FROM miss),
           (SELECT count(*) FROM extra),
           (SELECT string_agg(format('%s:%s%s', grantee_name, privilege_type,
                                     CASE WHEN is_grantable THEN ' WGO' ELSE '' END),
                              ', ' ORDER BY grantee_name, privilege_type)
              FROM miss),
           (SELECT string_agg(format('%s:%s%s', grantee_name, privilege_type,
                                     CASE WHEN is_grantable THEN ' WGO' ELSE '' END),
                              ', ' ORDER BY grantee_name, privilege_type)
              FROM extra)
      INTO n_missing, n_extra, s_missing, s_extra;

    IF n_missing > 0 OR n_extra > 0 THEN
      RAISE EXCEPTION
        '014 ACL replay did not restore %.% exactly: % privilege(s) LOST [%], % privilege(s) ADDED [%]. Refusing to commit a migration that silently changed who can read the reporting layer.',
        v_schema, v.view_name,
        n_missing, coalesce(s_missing, '-'),
        n_extra,   coalesce(s_extra,   '-');
    END IF;

    RAISE NOTICE '014 ACL replay: %.% restored exactly (owner %, % non-owner privilege(s)).',
      v_schema, v.view_name, v.view_owner,
      (SELECT count(*)
         FROM aclexplode(v.view_acl) a
        WHERE a.grantee <> (SELECT c.relowner
                              FROM pg_class c
                              JOIN pg_namespace ns ON ns.oid = c.relnamespace
                             WHERE ns.nspname = v_schema
                               AND c.relname  = v.view_name));
  END LOOP;

  IF n_regrantor > 0 THEN
    RAISE NOTICE
      '014 ACL replay: % grant(s) changed grantor to %. Privileges are intact; re-issue them from the original grantor if the grantor identity matters to you.',
      n_regrantor, current_user;
  END IF;
END $do$;

DROP TABLE IF EXISTS pg_temp.v013_view_acls;

COMMIT;
