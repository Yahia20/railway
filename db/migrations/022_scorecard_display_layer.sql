-- 022 — always show the score, always show what it rests on.
--
-- ═══════════════════════════════════════════════════════════════════════════
-- THE DECISION, AND WHY IT IS SAFE TO IMPLEMENT THIS WAY
--
-- The owner has decided, with the statistical caveats explained and
-- understood, that a visible provisional number labelled honestly is more
-- useful to him than a blank cell. A score is ALWAYS shown; beside it, always
-- how many evaluations it rests on, and that the method is interim.
--
-- WHAT THIS FILE DOES NOT DO, deliberately:
--
--   * it does not set min_n_publish to 0
--   * it does not delete or weaken band_stable
--   * it does not INSERT a fabricated row into eval_noise_params
--   * it does not edit migration 014, or the two views it defines
--
-- Every one of those would destroy the machinery 014 built to answer "is this
-- number trustworthy", and none of them could be cleanly undone when the A/A
-- re-measurement is finally taken. `band_stable` keeps computing exactly what
-- it computes today. `min_n_publish` stays 30. What changes is that they stop
-- being GATES and become INPUTS TO A LABEL.
--
-- HOW: a wrapper view, not a rewrite. `v_agent_scorecard_display` selects
-- `v_agent_scorecard.*` and appends label columns. The base view is untouched,
-- so there is no 300-line copy of it to drift, the grants and ownership 014
-- set are unaffected, and deleting this file's objects restores the previous
-- behaviour exactly.
--
-- WHY IT IS SAFE TO MAKE THESE MEANS VISIBLE. Verified before writing this:
-- `pg_depend` shows NOTHING in the database depends on either scorecard view;
-- no function references `v_agent_scorecard`, `v_quality_by_input`,
-- `avg_score` or `band_stable`; `evaluate_alert_rules()` keys on pass-1
-- classification (complaint, hot lead, promises) and reads no agent mean at
-- all; and no n8n node queries either view. They are pure leaf reporting
-- views. A provisional score therefore CANNOT reach an alert, a follow-up
-- trigger, a ranking or an export by inheritance -- the only way it travels is
-- if somebody writes a new consumer, and `is_provisional` is there so that
-- consumer can refuse it.
-- ═══════════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1 · the label, as one function so every view says the same words
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION eval_confidence_label(p_n_usable bigint,
                                                 p_band_stable boolean)
RETURNS text
LANGUAGE sql STABLE
AS $fn$
  SELECT CASE
    WHEN coalesce(p_n_usable, 0) = 0
      THEN 'no evaluations yet'
    WHEN p_n_usable < eval_report_param('min_n_publish')
      THEN 'provisional — ' || p_n_usable || ' evaluation'
           || CASE WHEN p_n_usable = 1 THEN '' ELSE 's' END
    WHEN NOT coalesce(p_band_stable, false)
      THEN 'provisional — margin of error not yet measured'
    ELSE 'published'
  END
$fn$;

COMMENT ON FUNCTION eval_confidence_label(bigint, boolean) IS
  'How much weight a reader may put on the mean sitting next to this. Reads min_n_publish from eval_report_params, so raising or lowering the publication threshold changes the LABEL and never hides the number. Four states: no evaluations yet / provisional (too few) / provisional (noise unmeasured) / published.';


-- `published` is the only state that is not provisional. Kept as its own
-- function so a consumer that must refuse an under-powered number tests one
-- thing, rather than re-deriving the rule and getting it subtly different.
CREATE OR REPLACE FUNCTION eval_is_provisional(p_n_usable bigint,
                                               p_band_stable boolean)
RETURNS boolean
LANGUAGE sql STABLE
AS $fn$
  SELECT eval_confidence_label(p_n_usable, p_band_stable) <> 'published'
$fn$;

COMMENT ON FUNCTION eval_is_provisional(bigint, boolean) IS
  'true unless the mean is fully published (enough usable scores AND a measured, stable band). ANY automated consumer -- an alert, a follow-up trigger, a ranking, an export -- must gate on this. Displaying a provisional score is the owner''s decision; acting on one automatically is not.';


-- ---------------------------------------------------------------------------
-- 2 · the method, in words
--
-- The scorecard emits ONE ROW PER AGENT PER VERSION CO-ORDINATE, which is
-- correct and must not be collapsed: two rows for the same person can be two
-- different scoring methods, and averaging them together compares a v1 score
-- with a v6 one. Until now the report showed neither the co-ordinate nor any
-- hint that it existed, so the same agent could appear twice with no
-- explanation and a reader would assume a bug or, worse, compare them.
--
-- The wording says "interim" because it IS: the rubric, the prompts and the
-- model have all changed during this project and will change again.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION eval_method_label(p_prompt_version text,
                                             p_rubric_version text,
                                             p_model text,
                                             p_model_fingerprint text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
  SELECT coalesce(p_prompt_version, 'unknown prompt')
      || ' · rubric ' || coalesce(p_rubric_version, '?')
      || ' · ' || coalesce(p_model, 'unknown model')
      -- The fingerprint distinguishes two runs of the "same" model that the
      -- provider served differently. Shown only when there is one, and
      -- shortened: it is an identity, not something anyone reads.
      || CASE WHEN p_model_fingerprint IS NULL OR p_model_fingerprint = ''
                THEN ''
              ELSE ' · build ' || left(p_model_fingerprint, 8) END
      || ' — interim method, will change'
$fn$;

COMMENT ON FUNCTION eval_method_label(text, text, text, text) IS
  'The four version co-ordinates as one readable string, so a reader can see at a glance that two rows for the same agent came from different scoring methods and must not be compared or averaged. Ends with "interim method, will change" because it is: prompts, rubric and model have all changed already.';


-- ---------------------------------------------------------------------------
-- 3 · the display views
--
-- `score_display` is a single jsonb object holding the number AND everything
-- that qualifies it. That shape is the point: a payload key can be dropped by
-- accident, but a caller that renders `score_display.headline` cannot show the
-- mean while losing the sample size, and a caller that reads `.value` has
-- `.label` sitting in the same object it already has in its hand.
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

COMMIT;
