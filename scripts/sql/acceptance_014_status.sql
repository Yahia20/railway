-- Verification and acceptance queries for db/migrations/014_evaluation_status.sql.
-- Every SQL statement quoted in docs/PR2-db-status.md lives here so it can be
-- reviewed and run without copying out of prose.
--
-- Round-5 revision (Sol adversarial review). New since the first acceptance
-- run: checks 8, 8b and 9 (F4 reporting-role privileges, F5 exact ACL replay),
-- schema-bound catalogue lookups in P1 / 1 / 2 / 3 / 3b / 6c (F20), and the F1
-- section relabelled as SQL integration rather than end-to-end (F16). Fresh
-- outputs from staging are in the scratch file ACCEPTANCE_013_RERUN.md.
--
-- All of it is READ-ONLY except sections F1, P4, P5 and P6, which are
-- explicitly marked MUTATES, create their own fixtures and roll back or clean
-- up. P6 (Sol's D1 amber rule) is the only one written to be run as it stands:
-- it is a BEGIN ... ROLLBACK block that builds its own agent, interactions,
-- transcripts and evaluations and depends on no production data.

-- ===========================================================================
-- PREFLIGHT — run BEFORE applying 014
-- ===========================================================================

-- P1 . 014 DROPs v_agent_scorecard and v_quality_by_input (their column shape
--      changes, and CREATE OR REPLACE can only append columns). The DROP is a
--      plain DROP, never CASCADE: if anything depends on either view the
--      migration must FAIL loudly rather than silently delete somebody's
--      report. Find those dependants first. Expect zero rows.
--      BOUND TO OBJECTS, NOT TO NAMES (round-5 review finding F20). The
--      earlier revision matched `source_view.relname IN (...)`, which finds a
--      view of that name in ANY schema, and excluded dependants by name, which
--      hides a REAL dependant that merely happens to share a name in another
--      schema. Both directions are now bound to the two specific relations
--      014 will drop -- the ones in current_schema(), resolved through
--      to_regclass -- so the answer cannot be about somebody else's
--      v_agent_scorecard. to_regclass (not ::regclass) because it returns NULL
--      instead of raising when the view does not exist yet, which is the
--      legitimate state of a database that has never had 007 applied.
SELECT dependent_ns.nspname   AS dependent_schema,
       dependent_view.relname AS dependent_object,
       source_ns.nspname      AS depends_on_schema,
       source_view.relname    AS depends_on
  FROM pg_depend d
  JOIN pg_rewrite r              ON r.oid = d.objid
  JOIN pg_class dependent_view   ON dependent_view.oid = r.ev_class
  JOIN pg_class source_view      ON source_view.oid = d.refobjid
  JOIN pg_namespace dependent_ns ON dependent_ns.oid = dependent_view.relnamespace
  JOIN pg_namespace source_ns    ON source_ns.oid    = source_view.relnamespace
 WHERE d.classid    = 'pg_rewrite'::regclass
   AND d.refclassid = 'pg_class'::regclass
   AND source_view.oid IN (
         to_regclass(quote_ident(current_schema()) || '.v_agent_scorecard'),
         to_regclass(quote_ident(current_schema()) || '.v_quality_by_input'))
   -- a view's own rewrite rule depends on the view itself; and the other view
   -- in the drop set is going anyway. Exclude BY OID, never by name.
   AND dependent_view.oid NOT IN (
         to_regclass(quote_ident(current_schema()) || '.v_agent_scorecard'),
         to_regclass(quote_ident(current_schema()) || '.v_quality_by_input'));
-- expect 0 rows. Any row here is a report that will break; decide what happens
-- to it BEFORE the migration, not during.

-- P2 . the size of the backfill over the WHOLE table, so the "before" number is
--      on record. This is NOT the number the scorecard reconciles against --
--      see P2b, which is.
SELECT count(*)                                AS evaluations_total,
       count(final_score)                      AS with_score,
       count(*) - count(final_score)           AS without_score
  FROM agent_evaluations;
-- Keep this. It is the whole table, including rows the scorecard cannot see.

-- P2b . THE LIKE-FOR-LIKE POPULATION (round-4 review finding, extended for
--       Sol's D1 amber rule). The earlier revision claimed P2's `without_score`
--       must equal ok_without_score_count summed over v_agent_scorecard. It
--       must not, and on any real database it will not: the scorecard INNER
--       JOINs interactions and agents and filters is_bot = false, so an
--       evaluation with a NULL agent_id, an agent row that is a bot, or a
--       missing interaction is simply not in it. Comparing the two was
--       comparing a table to a filtered view of it.
--
--       THE D1 RULE ADDS A SECOND EXCLUSION, and it is the bigger one. A call
--       evaluation whose transcript is not 'green' is not in
--       evaluated_interactions either -- it is in amber_shadow_count, which is
--       OUTSIDE the five-bucket partition. So the like-for-like population now
--       carries the ASR gate as well, and P2c below splits the remainder into
--       "not a human agent's" and "not green".
--
--       These numbers are computed over EXACTLY the rows v_agent_scorecard
--       counts in evaluated_interactions. `without_score` HERE is what must
--       equal sum(ok_without_score_count) immediately after 014, and must not
--       grow afterwards. `asr_shadowed` must equal sum(amber_shadow_count)
--       (query R2b).
SELECT count(*) FILTER (WHERE x.asr_eligible)                    AS scorecard_visible_total,
       count(e.final_score) FILTER (WHERE x.asr_eligible)        AS with_score,
       count(*) FILTER (WHERE x.asr_eligible AND e.final_score IS NULL)
                                                                 AS without_score,
       count(*) FILTER (WHERE NOT x.asr_eligible)                AS asr_shadowed
  FROM agent_evaluations e
  JOIN interactions i ON i.interaction_id = e.interaction_id
  JOIN agents a       ON a.agent_id = e.agent_id
  -- One transcript per interaction: transcripts.interaction_id is NOT NULL
  -- UNIQUE (003_interactions.sql), so this join cannot duplicate a row. Checks
  -- 6c and 6d below re-assert that against the live catalogue.
  LEFT JOIN transcripts t ON t.interaction_id = e.interaction_id
  CROSS JOIN LATERAL (
    SELECT eval_asr_input_is_eligible(e.input_type,
                                      t.asr_metrics->>'asr_quality_status')
             AS asr_eligible
  ) x
 WHERE a.is_bot = false;
-- NOTE: this query calls eval_asr_input_is_eligible(), which 014 creates. Run
-- it as a preflight by pasting the predicate inline:
--   coalesce(e.input_type <> 'call_transcript'
--            OR t.asr_metrics->>'asr_quality_status' = 'green', false)
-- and re-run it as written afterwards; the two must agree.

-- P2c . and the remainder, so the gap between P2 and P2b is a number somebody
--       looked at rather than a discrepancy somebody explains away later.
SELECT count(*) FILTER (WHERE e.agent_id IS NULL)             AS null_agent,
       count(*) FILTER (WHERE a.agent_id IS NOT NULL
                          AND a.is_bot)                       AS bot_agent,
       count(*) FILTER (WHERE i.interaction_id IS NULL)       AS orphan_interaction,
       count(*) FILTER (WHERE e.agent_id IS NOT NULL
                          AND a.agent_id IS NULL)             AS agent_id_not_in_agents,
       -- The D1 breakdown, over the WHOLE table (not only the scorecard's
       -- population): why each call evaluation is or is not publishable.
       count(*) FILTER (WHERE e.input_type = 'call_transcript'
                          AND t.asr_metrics->>'asr_quality_status' = 'green')
                                                              AS call_green,
       count(*) FILTER (WHERE e.input_type = 'call_transcript'
                          AND t.asr_metrics->>'asr_quality_status' = 'amber')
                                                              AS call_amber,
       count(*) FILTER (WHERE e.input_type = 'call_transcript'
                          AND t.asr_metrics->>'asr_quality_status' = 'red')
                                                              AS call_red,
       count(*) FILTER (WHERE e.input_type = 'call_transcript'
                          AND t.interaction_id IS NOT NULL
                          AND t.asr_metrics->>'asr_quality_status' IS NULL)
                                                              AS call_status_missing,
       count(*) FILTER (WHERE e.input_type = 'call_transcript'
                          AND t.interaction_id IS NULL)       AS call_no_transcript,
       count(*) FILTER (WHERE e.input_type = 'call_transcript'
                          AND t.asr_metrics->>'asr_quality_status'
                              NOT IN ('green', 'amber', 'red'))
                                                              AS call_status_unknown_value,
       count(*) FILTER (WHERE e.input_type <> 'call_transcript') AS chats
  FROM agent_evaluations e
  LEFT JOIN interactions i ON i.interaction_id = e.interaction_id
  LEFT JOIN agents a       ON a.agent_id = e.agent_id
  LEFT JOIN transcripts t  ON t.interaction_id = e.interaction_id;
-- P2 total = P2b total + (these, without double counting a row that is more
-- than one of them). The point is not an identity, it is that every row the
-- scorecard drops is dropped for a stated reason.
--
-- READ call_amber, call_status_missing, call_no_transcript AND
-- call_status_unknown_value BEFORE APPLYING 014. Together they are how much of
-- the call history stops being published the moment this migration lands. If
-- that number is most of the corpus, the answer is to re-transcribe, not to
-- weaken the rule -- but nobody should discover the size of it from a dashboard
-- that went empty. call_status_unknown_value should be 0: a non-empty value
-- means some writer is putting a fourth status in asr_metrics and
-- eval_asr_input_is_eligible() is (correctly, fail-closed) excluding it.

-- P3 . VIEW OWNER AND GRANTS, BEFORE THE DROP (round-4 review finding).
--      DROP + CREATE makes NEW objects, and new objects have no ACL: a
--      reporting role that could SELECT these views this morning loses that
--      privilege silently. 014 snapshots this into a temp table and replays it
--      after the CREATE, inside the same transaction -- but a temp table is not
--      a record. Run this, keep the output, and compare it to the post-check in
--      section 7 below.
SELECT c.relname                              AS view_name,
       pg_get_userbyid(c.relowner)            AS view_owner,
       coalesce(array_to_string(c.relacl, E'\n'), '(no explicit ACL)') AS acl
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'v'
   AND n.nspname = current_schema()
   AND c.relname IN ('v_agent_scorecard', 'v_quality_by_input')
 ORDER BY c.relname;

-- P4 . can the migration role actually do what 014 needs? MUTATES NOTHING, but
--      it answers the two questions that would otherwise abort the transaction
--      halfway: 014 disables and re-enables t_eval_updated around the backfill
--      (needs ownership of agent_evaluations) and re-owns the two views (needs
--      ownership of them, or membership in the owning role).
SELECT current_user                                                AS running_as,
       pg_get_userbyid(c.relowner)                                 AS object_owner,
       c.relname                                                   AS object,
       pg_has_role(current_user, c.relowner, 'USAGE')               AS can_set_owner
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema()
   AND c.relname IN ('agent_evaluations', 'v_agent_scorecard', 'v_quality_by_input')
 ORDER BY c.relname;
-- can_set_owner must be true on all three. False on agent_evaluations means the
-- DISABLE TRIGGER in section 2 will raise and roll the whole migration back.

-- ===========================================================================
-- AFTER applying 014
-- ===========================================================================

-- 1 . the five new columns exist, with the right defaults and nullability
-- table_schema is bound as well (F20): information_schema.columns spans every
-- schema, and five same-named columns on somebody else's agent_evaluations
-- would false-pass this.
SELECT column_name, data_type, is_nullable, column_default
  FROM information_schema.columns
 WHERE table_schema = current_schema()
   AND table_name   = 'agent_evaluations'
   AND column_name IN ('contract_status', 'gradeable', 'ungradeable_modules',
                       'evidence_rejected', 'model_fingerprint')
 ORDER BY column_name;
-- expect 5 rows:
--   contract_status      text     NO   'ok'::text
--   evidence_rejected    jsonb    NO   '[]'::jsonb
--   gradeable            boolean  NO   true
--   model_fingerprint    text     YES  (null)
--   ungradeable_modules  jsonb    NO   '[]'::jsonb

-- 2 . the status CHECK exists under a findable name and lists all four values
SELECT conname, pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conrelid = to_regclass(quote_ident(current_schema())
                              || '.agent_evaluations')   -- F20: not search_path
   AND contype = 'c'
   AND conname = 'agent_evaluations_contract_status_check';
-- expect one row naming ok / contract_failed / ungradeable / unscoreable

-- 3 . the index
SELECT schemaname, indexname, indexdef
  FROM pg_indexes
 WHERE schemaname = current_schema()          -- F20: pg_indexes spans schemas
   AND tablename  = 'agent_evaluations'
   AND indexname  = 'idx_agent_evaluations_contract_status';
-- expect (contract_status, gradeable)

-- 3b . the backfill did NOT rewrite updated_at. 014 disables t_eval_updated
--      around it precisely so this stays true; the trigger must be back on
--      afterwards or every later write stops maintaining the column.
SELECT tgname,
       tgenabled                     AS enabled_flag,
       tgenabled = 'O'               AS enabled_normally
  FROM pg_trigger
 WHERE tgrelid = to_regclass(quote_ident(current_schema())
                             || '.agent_evaluations')    -- F20: not search_path
   AND NOT tgisinternal;
-- expect t_eval_updated with tgenabled = 'O' (origin -- i.e. on). A 'D' here
-- means the migration failed between the DISABLE and the ENABLE, which cannot
-- happen inside the transaction but can if somebody ran the file in pieces.

SELECT count(*) AS rows_stamped_today_without_a_writer
  FROM agent_evaluations
 WHERE final_score IS NULL
   AND updated_at::date = current_date;
-- Sanity check on the same thing, from the data side. Run it on migration day:
-- a large number here, on a database whose workflow has been off since step 1,
-- means the backfill stamped the history after all.

-- 4 . the backfill invariant: NOTHING claims to be graded without a score
SELECT count(*) AS must_be_zero
  FROM agent_evaluations
 WHERE gradeable = true AND final_score IS NULL;
-- expect 0. This is the invariant 014's backfill establishes and both writing
-- nodes maintain ('Store evaluation' and 'Store unscoreable outcome' AND
-- gradeable with "a score arrived"). A non-zero here after rollout means a
-- writer is not going through those nodes -- workflows 01 / 01b are the
-- candidates.

-- 5 . the parameters are seeded and readable, in the two tables they belong in
SELECT param_key, value, source FROM eval_report_params ORDER BY param_key;
-- expect min_n_publish = 30, z_95 = 1.959964

SELECT param_key, prompt_version, rubric_version, model, model_fingerprint,
       value, measured_on
  FROM eval_noise_params
 ORDER BY param_key, prompt_version, model;
-- expect exactly one row:
--   repeat_run_variance | pass2-agent-quality-v3 | 1.0.0 | deepseek-chat
--                       | (null fingerprint)     | 188.70 | 2026-08-13

-- 5b . THE CO-ORDINATE BINDING IS REAL (round-4 review finding). The noise
--      measurement answers only for the co-ordinate it was measured on. This is
--      the query that proves a re-measurement cannot leak sideways into a
--      version group it never described.
SELECT eval_noise_param('repeat_run_variance', 'pass2-agent-quality-v3',
                        '1.0.0', 'deepseek-chat', NULL)        AS exact_expect_188_70,
       eval_noise_param('repeat_run_variance', 'pass2-agent-quality-v3',
                        '1.0.0', 'deepseek-chat', 'fp_anything') AS wildcard_expect_188_70,
       -- substitute whatever judge.PASS2_VERSION is on the day you run this;
       -- v6 is the value at the time of writing and it has moved twice already.
       eval_noise_param('repeat_run_variance', 'pass2-agent-quality-v6',
                        '1.0.0', 'deepseek-v4-flash', NULL)    AS shipping_expect_null,
       eval_noise_param('repeat_run_variance', 'pass2-agent-quality-v3',
                        '2.0.0', 'deepseek-chat', NULL)        AS other_rubric_expect_null;
-- expect 188.70, 188.70, NULL, NULL.
--
-- `shipping_expect_null` being NULL is NOT a defect. The worker ships the
-- current pass-2 prompt against deepseek-v4-flash and nobody has measured the
-- repeat-run noise on that co-ordinate, so nothing on it is publishable until
-- the A/A run is repeated and its variance INSERTed. See docs/PR2-db-status.md
-- section 3, rule 2.

-- 5c . the two half-widths, and the difference between them
SELECT eval_noise_floor_half_width_95(30,  188.70)              AS floor_at_30,
       eval_noise_floor_half_width_95(81,  188.70)              AS floor_at_81,
       eval_noise_floor_half_width_95(182, 188.70)              AS floor_at_182,
       eval_ci_half_width_95(30,  100.00, 188.70)               AS ci_floor_wins,
       eval_ci_half_width_95(30,  400.00, 188.70)               AS ci_spread_wins,
       eval_ci_half_width_95(30,  NULL,   188.70)               AS ci_no_sample_var,
       eval_ci_half_width_95(30,  400.00, NULL)                 AS ci_unmeasured_coord,
       eval_ci_half_width_95(0,   400.00, 188.70)               AS ci_no_rows;
-- expect 4.92, 2.99, 2.00, 4.92, 7.16, 4.92, NULL, NULL
--
-- `ci_floor_wins` = `floor_at_30`: with an observed variance of 100 the judge
-- noise is the larger of the two and the floor stands.
-- `ci_spread_wins` is bigger than the floor: with an observed variance of 400
-- the real between-call spread dominates, which is what the round-4 review
-- required and what the first draft omitted entirely.
-- `ci_unmeasured_coord` NULL is the fail-closed path: no measured noise floor
-- for this co-ordinate, so no interval, so no publishable band.

-- 6 . the usable-score rule is a function, and it is the only definition
SELECT eval_score_is_usable('ok',              true,  80)   AS t_expect_true,
       eval_score_is_usable('ok',              true,  NULL) AS f_no_score,
       eval_score_is_usable('ok',              false, 80)   AS f_not_gradeable,
       eval_score_is_usable('ungradeable',     false, NULL) AS f_ungradeable,
       eval_score_is_usable('unscoreable',     false, NULL) AS f_unscoreable,
       eval_score_is_usable('contract_failed', true,  80)   AS f_contract_failed;
-- expect true, false, false, false, false, false

-- 6b . SOL'S D1 RULE IS ALSO A FUNCTION, AND IT FAILS CLOSED. Every case that
--      is not literally 'green' on a call must come back false, INCLUDING the
--      NULLs -- which is the one behaviour that differs from 013's
--      evaluate_alert_rules(), where a missing status coalesces to 'green'.
SELECT eval_asr_input_is_eligible('chat',            NULL)      AS t_chat_no_transcript,
       eval_asr_input_is_eligible('chat',            'amber')   AS t_chat_ignores_asr,
       eval_asr_input_is_eligible('call_transcript', 'green')   AS t_call_green,
       eval_asr_input_is_eligible('call_transcript', 'amber')   AS f_call_amber,
       eval_asr_input_is_eligible('call_transcript', 'red')     AS f_call_red,
       eval_asr_input_is_eligible('call_transcript', NULL)      AS f_call_no_status,
       eval_asr_input_is_eligible('call_transcript', 'GREEN')   AS f_call_case_matters,
       eval_asr_input_is_eligible('call_transcript', 'chartreuse')
                                                                AS f_call_unknown_value,
       eval_asr_input_is_eligible(NULL,              'green')   AS f_null_input_type;
-- expect true, true, true, false, false, false, false, false, false
--
-- `f_call_case_matters` false is deliberate, not an oversight: the rule is
-- "exactly 'green'". services/worker/app/asr/text_quality.py emits lower case
-- and nothing else; if that ever changes, this check goes red rather than the
-- scorecard quietly re-admitting bad calls.
-- `f_null_input_type` false is the coalesce doing its job -- input_type is NOT
-- NULL on agent_evaluations, so this can only happen if somebody calls the
-- function from somewhere it does not belong.

-- 6c . THE JOIN CANNOT FAN A ROW OUT. All three views LEFT JOIN transcripts on
--      interaction_id and rely on there being at most one match, which is why
--      none of them needs a "pick the current transcript" tie-break. That is a
--      UNIQUE constraint in 003_interactions.sql, not a convention. Assert it.
--      BOUND TO ONE RELATION, NOT TO A NAME (round-5 review finding F20). The
--      earlier revision matched `t.relname = 'transcripts'` in ANY schema, so
--      a `staging.transcripts` or an `archive.transcripts` carrying that
--      UNIQUE would have made this check pass while the table the three views
--      actually read carried nothing. conrelid is now pinned to the
--      transcripts in current_schema() -- which is the one 014's views were
--      created against.
SELECT count(*) AS unique_constraints_expect_at_least_1
  FROM pg_constraint c
 WHERE c.conrelid = to_regclass(quote_ident(current_schema()) || '.transcripts')
   AND c.contype IN ('u', 'p')
   -- attname is `name`, not text, and there is no name[] = text[] operator
   -- (PG 16/17 both raise "operator does not exist"). Cast BOTH sides to text[]
   -- rather than relying on a literal being inferred as name[].
   AND (SELECT array_agg(a.attname::text ORDER BY a.attname::text)
          FROM unnest(c.conkey) k
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k)
       = ARRAY['interaction_id']::text[];
-- expect 1: the UNIQUE on interaction_id. The primary key is on transcript_id,
-- so it is correctly NOT counted here.
--
-- ZERO MEANS THE VIEWS ARE WRONG, not that this check is stale:
-- with two transcripts per interaction the LEFT JOIN duplicates every
-- evaluation and both scorecard counts and shadow counts double. Fix by turning
-- each `LEFT JOIN transcripts t ON t.interaction_id = e.interaction_id` into a
-- LATERAL taking the newest transcribed_at, and say so in docs/PR2-db-status.md.

-- 6d . and the same thing empirically, in case a future migration drops the
--      constraint without anybody re-reading 014.
SELECT count(*) AS must_be_zero
  FROM (SELECT interaction_id FROM transcripts     -- current_schema(), as 6c
         GROUP BY interaction_id HAVING count(*) > 1) d;
-- expect 0. Unqualified on purpose: this must read whatever `transcripts`
-- resolves to under the SAME search_path 014's views were created under, and
-- 6c above proves that relation is the one carrying the UNIQUE.

-- 7 . THE GRANTS SURVIVED THE DROP. Compare this output to preflight P3, row
--     for row. A view that lost its ACL is a dashboard that will 403 tomorrow.
SELECT c.relname                              AS view_name,
       pg_get_userbyid(c.relowner)            AS view_owner,
       coalesce(array_to_string(c.relacl, E'\n'), '(no explicit ACL)') AS acl
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'v'
   AND n.nspname = current_schema()
   AND c.relname IN ('v_agent_scorecard', 'v_quality_by_input')
 ORDER BY c.relname;
-- Must be identical to P3. If P3 said "(no explicit ACL)" this must too.
--
-- "Identical" means identical MODULO THE OWNER'S OWN ENTRY. If the database
-- carries ALTER DEFAULT PRIVILEGES that stamp grants onto newly created
-- relations, 014's replay has to REVOKE them (section 5b), and the first
-- REVOKE materialises an ACL on a view whose relacl was previously NULL: P3
-- prints "(no explicit ACL)" and this prints `{postgres=arwdDxt/postgres}`,
-- which is the same privileges written a different way. Section 9 below is the
-- check that compares the two the right way -- as a multiset of NON-OWNER
-- grants -- and it is the one to believe when the two texts differ.

-- ---------------------------------------------------------------------------
-- 8 . A VIEW-ONLY REPORTING ROLE CAN ACTUALLY READ THE REPORTS
--     (round-5 review finding F4).  MUTATES, AND CLEANS ITSELF UP: the whole
--     section is a BEGIN ... ROLLBACK block that creates its own role. Run it
--     as it stands.
-- ---------------------------------------------------------------------------
-- WHY THIS CHECK EXISTS. A view runs its body with the VIEW OWNER's rights, so
-- granting SELECT on v_agent_scorecard is enough to let a role read
-- agent_evaluations through it. A FUNCTION called from that view body does NOT
-- work that way: a SECURITY INVOKER function runs as the CALLER. Every row of
-- v_agent_scorecard and v_quality_by_input calls eval_noise_param(), which
-- reads eval_noise_params, and every half-width calls eval_report_param(),
-- which reads eval_report_params. Before F4 both were SECURITY INVOKER, so a
-- future metabase_ro / reporter role holding nothing but SELECT on three views
-- would have got `permission denied for table eval_noise_params` from the
-- scorecard -- and nobody would have found out until the dashboard broke,
-- because the migration role that ran every earlier check is the owner and can
-- read everything.
--
-- THE FIX IS SECURITY DEFINER, NOT A GRANT. `GRANT SELECT ON eval_noise_params
-- TO PUBLIC` would also have worked, and would also have kept the table
-- non-writable -- but it publishes the table to every role in the cluster and
-- has to be re-issued by hand after any rebuild of it. SECURITY DEFINER leaves
-- the two parameter tables invisible and reachable only through one read-only
-- parameterised lookup, which is the stricter choice. This check therefore
-- asserts BOTH halves: the reporter CAN read the three views and CAN call the
-- lookups, and CANNOT read or write the tables behind them.
BEGIN;

CREATE ROLE acc_reporter NOLOGIN;
GRANT USAGE ON SCHEMA public TO acc_reporter;
GRANT SELECT ON v_agent_scorecard, v_quality_by_input, v_usable_evaluations
   TO acc_reporter;

-- 8a . what the role holds, before assuming it. Everything named
--      `_must_be_false` is a privilege deliberately NOT granted.
SELECT 'acc_reporter'                                                  AS role,
       has_table_privilege('acc_reporter', 'public.v_agent_scorecard',
                           'SELECT')                                   AS scorecard_select_must_be_true,
       has_table_privilege('acc_reporter', 'public.eval_noise_params',
                           'SELECT')                                   AS noise_select_must_be_false,
       has_table_privilege('acc_reporter', 'public.eval_noise_params',
                           'INSERT')                                   AS noise_insert_must_be_false,
       has_table_privilege('acc_reporter', 'public.eval_noise_params',
                           'UPDATE')                                   AS noise_update_must_be_false,
       has_table_privilege('acc_reporter', 'public.eval_report_params',
                           'SELECT')                                   AS report_select_must_be_false,
       has_table_privilege('acc_reporter', 'public.agent_evaluations',
                           'SELECT')                                   AS raw_select_must_be_false,
       has_function_privilege('acc_reporter',
         'public.eval_noise_param(text,text,text,text,text)', 'EXECUTE')
                                                                       AS noise_fn_execute_must_be_true;
-- expect: true, false, false, false, false, false, true

-- 8b . and now BE the role. Every statement below runs as acc_reporter.
SET ROLE acc_reporter;
SELECT current_user AS now_running_as;      -- must print acc_reporter

DO $acc8$
DECLARE
  n_scorecard bigint;
  n_quality   bigint;
  n_usable    bigint;
  v_noise     numeric;
  v_hw        numeric;
BEGIN
  -- 1 . the three views must be readable. A missing GRANT or a SECURITY
  --     INVOKER lookup shows up here as insufficient_privilege, which is not
  --     caught and therefore fails the check.
  SELECT count(*) INTO n_scorecard FROM public.v_agent_scorecard;
  SELECT count(*) INTO n_quality   FROM public.v_quality_by_input;
  SELECT count(*) INTO n_usable    FROM public.v_usable_evaluations;
  RAISE NOTICE 'check 8b: acc_reporter read v_agent_scorecard (% rows), v_quality_by_input (% rows), v_usable_evaluations (% rows).',
    n_scorecard, n_quality, n_usable;

  -- 2 . the two definer lookups must work FOR THIS ROLE, with real values --
  --     an empty scorecard would otherwise let a broken lookup pass unnoticed.
  v_noise := public.eval_noise_param('repeat_run_variance',
                                     'pass2-agent-quality-v3', '1.0.0',
                                     'deepseek-chat', NULL);
  IF v_noise IS DISTINCT FROM 188.70 THEN
    RAISE EXCEPTION 'check 8b FAILED: eval_noise_param() returned % as acc_reporter, expected 188.70.', v_noise;
  END IF;

  v_hw := public.eval_ci_half_width_95(100, 250.0, 188.70);
  IF v_hw IS NULL THEN
    RAISE EXCEPTION 'check 8b FAILED: eval_ci_half_width_95() returned NULL as acc_reporter -- eval_report_param(''z_95'') is not readable by this role.';
  END IF;
  RAISE NOTICE 'check 8b: eval_noise_param -> %, eval_ci_half_width_95(100, 250.0, 188.70) -> % (both computed as acc_reporter).',
    v_noise, v_hw;

  -- 3 . and the tables behind them must STAY shut. SECURITY DEFINER is only
  --     the right answer if it did not also hand the reporter the table.
  BEGIN
    PERFORM count(*) FROM public.eval_noise_params;
    RAISE EXCEPTION 'check 8b FAILED: acc_reporter could SELECT eval_noise_params directly. The definer functions were supposed to be the only way in.';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'check 8b: direct SELECT on eval_noise_params correctly denied to acc_reporter.';
  END;
  BEGIN
    PERFORM count(*) FROM public.eval_report_params;
    RAISE EXCEPTION 'check 8b FAILED: acc_reporter could SELECT eval_report_params directly.';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'check 8b: direct SELECT on eval_report_params correctly denied to acc_reporter.';
  END;
  BEGIN
    PERFORM count(*) FROM public.agent_evaluations;
    RAISE EXCEPTION 'check 8b FAILED: acc_reporter could SELECT agent_evaluations directly. It was granted three VIEWS and nothing else.';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'check 8b: direct SELECT on agent_evaluations correctly denied to acc_reporter.';
  END;

  RAISE NOTICE 'check 8b PASS.';
END
$acc8$;

RESET ROLE;
SELECT current_user AS back_to;              -- must print the migration role

-- The role and every grant made above disappear with the ROLLBACK: CREATE ROLE
-- is transactional in PostgreSQL, so there is nothing left to drop by hand.
ROLLBACK;

-- 8c . self-cleaning, proved. Run this AFTER the ROLLBACK above.
SELECT count(*) AS acc_reporter_must_be_zero
  FROM pg_roles WHERE rolname = 'acc_reporter';
-- expect 0

-- 8d . the two lookups really are definer functions with a pinned search_path,
--      and the parameter tables really are ungranted. This is the static form
--      of check 8 -- it holds even on a database where you cannot create a
--      role.
SELECT p.proname,
       p.prosecdef                                   AS security_definer_must_be_true,
       pg_get_userbyid(p.proowner)                   AS owner,
       coalesce(array_to_string(p.proconfig, ', '),
                '(no SET clause)')                   AS settings_must_pin_search_path
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = current_schema()
   AND p.proname IN ('eval_report_param', 'eval_noise_param')
 ORDER BY p.proname;
-- expect two rows, prosecdef = true on both, settings = search_path=pg_catalog, public

SELECT c.relname                                                    AS param_table,
       coalesce(array_to_string(c.relacl, E'\n'), '(no explicit ACL)')
                                                                    AS acl_should_stay_owner_only
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema()
   AND c.relname IN ('eval_noise_params', 'eval_report_params')
 ORDER BY c.relname;
-- expect "(no explicit ACL)" on both, i.e. nobody but the owner. A GRANT
-- appearing here is somebody undoing F4 by the other route; that is a
-- decision, not a bug, but it must be a deliberate one.

-- ---------------------------------------------------------------------------
-- 9 . THE ACL REALLY SURVIVES A DROP/CREATE, AND NOTHING ELSE ARRIVES WITH IT
--     (round-5 review findings F5 and F12).  MUTATES.  Two committed steps
--     with a real 014 run between them, then a teardown. There is no
--     single-transaction form of this check: 014 carries its own BEGIN/COMMIT,
--     so it cannot run inside one.
-- ---------------------------------------------------------------------------
-- WHY IT COULD NOT BE PROVED BEFORE. The production dump used to build the
-- staging copy was taken with --no-acl, so every view arrived with relacl NULL
-- and the snapshot/replay in 014 section 5b had literally nothing to carry.
-- "The grants survived" was a true statement about an empty set. This section
-- puts a real, deliberately awkward grant set on the views first, so the
-- mechanism is exercised rather than asserted:
--
--   * a plain SELECT to one role                 -- the ordinary case
--   * a SELECT WITH GRANT OPTION to another      -- is_grantable must survive
--   * a SELECT to PUBLIC on the second view      -- grantee 0 must survive
--   * ALTER DEFAULT PRIVILEGES granting SELECT AND INSERT on new tables
--        -- the case that BREAKS a GRANT-only replay. The CREATE VIEW inside
--        014 inherits those, so after the migration the views would carry an
--        INSERT that was never in the snapshot unless 5b clears first.
--
-- RUN IT LIKE THIS (psql, three steps):
--
--   -- step 1 (this file, committed):
--   \i scripts/sql/acceptance_014_status.sql      -- or paste 9a below
--   -- step 2 (a separate psql invocation):
--   psql -v ON_ERROR_STOP=1 -f db/migrations/014_evaluation_status.sql
--   -- step 3 (back here): run 9b, then 9c.
--
-- 9a . SET UP THE SYNTHETIC GRANTS. Committed on purpose -- 014 has to see
--      them. MUTATES: three roles and four grants, all removed by 9c.
-- BEGIN;
-- CREATE ROLE acc_acl_reader   NOLOGIN;
-- CREATE ROLE acc_acl_delegate NOLOGIN;
-- GRANT USAGE ON SCHEMA public TO acc_acl_reader, acc_acl_delegate;
-- GRANT SELECT ON v_agent_scorecard  TO acc_acl_reader;
-- GRANT SELECT ON v_agent_scorecard  TO acc_acl_delegate WITH GRANT OPTION;
-- GRANT SELECT ON v_quality_by_input TO PUBLIC;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public
--   GRANT SELECT, INSERT ON TABLES TO acc_acl_reader;
-- -- record the "before" picture as a REAL table (014 drops pg_temp objects):
-- DROP TABLE IF EXISTS acc9_before;
-- CREATE TABLE acc9_before AS
-- SELECT c.relname AS view_name,
--        CASE WHEN a.grantee = 0 THEN 'PUBLIC'
--             ELSE pg_get_userbyid(a.grantee) END AS grantee_name,
--        a.privilege_type,
--        a.is_grantable
--   FROM pg_class c
--   JOIN pg_namespace n ON n.oid = c.relnamespace
--   CROSS JOIN LATERAL aclexplode(c.relacl) a
--  WHERE n.nspname = current_schema()
--    AND c.relkind = 'v'
--    AND c.relname IN ('v_agent_scorecard', 'v_quality_by_input')
--    AND a.grantee <> c.relowner;
-- COMMIT;
--
-- 9b . AFTER re-running 014 -- the assertion. Both counts must be zero.
--      `lost` is a privilege the DROP destroyed; `gained` is a privilege the
--      CREATE inherited and the replay failed to clear (the ALTER DEFAULT
--      PRIVILEGES INSERT is the one to watch).
-- WITH now AS (
--   SELECT c.relname AS view_name,
--          CASE WHEN a.grantee = 0 THEN 'PUBLIC'
--               ELSE pg_get_userbyid(a.grantee) END AS grantee_name,
--          a.privilege_type,
--          a.is_grantable
--     FROM pg_class c
--     JOIN pg_namespace n ON n.oid = c.relnamespace
--     CROSS JOIN LATERAL aclexplode(c.relacl) a
--    WHERE n.nspname = current_schema()
--      AND c.relkind = 'v'
--      AND c.relname IN ('v_agent_scorecard', 'v_quality_by_input')
--      AND a.grantee <> c.relowner
-- )
-- SELECT (SELECT count(*) FROM (TABLE acc9_before EXCEPT ALL TABLE now) x) AS lost_must_be_zero,
--        (SELECT count(*) FROM (TABLE now EXCEPT ALL TABLE acc9_before) x) AS gained_must_be_zero,
--        (SELECT count(*) FROM acc9_before)                                AS grants_exercised;
-- -- expect 0, 0, 3  (v_agent_scorecard: reader/SELECT and delegate/SELECT
-- -- WITH GRANT OPTION; v_quality_by_input: PUBLIC/SELECT. Three aclexplode
-- -- rows -- the grant option is a flag on the delegate row, not a fourth row,
-- -- and the ALTER DEFAULT PRIVILEGES INSERT is deliberately NOT in `before`:
-- -- it is the privilege the replay must CLEAR, so it belongs in `gained` if
-- -- 5b regresses. `grants_exercised` is whatever 9a actually put on, printed
-- -- so a zero there cannot masquerade as a pass.)
--
-- -- and the same thing in full, for the record:
-- SELECT * FROM acc9_before ORDER BY view_name, grantee_name, privilege_type;
--
-- 9c . TEARDOWN. Leaves the database exactly as 9a found it.
-- BEGIN;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public
--   REVOKE SELECT, INSERT ON TABLES FROM acc_acl_reader;
-- REVOKE ALL ON v_agent_scorecard, v_quality_by_input
--   FROM acc_acl_reader, acc_acl_delegate, PUBLIC;
-- REVOKE USAGE ON SCHEMA public FROM acc_acl_reader, acc_acl_delegate;
-- DROP TABLE IF EXISTS acc9_before;
-- DROP ROLE IF EXISTS acc_acl_reader;
-- DROP ROLE IF EXISTS acc_acl_delegate;
-- COMMIT;
-- SELECT count(*) AS leftover_roles_must_be_zero FROM pg_roles
--  WHERE rolname IN ('acc_acl_reader', 'acc_acl_delegate');
--
-- NOTE ON `REVOKE ... FROM PUBLIC` in 9c: v_quality_by_input's relacl is NOT
-- NULL again afterwards -- it will read `{postgres=arwdDxt/postgres}` rather
-- than "(no explicit ACL)". That is expected and is why section 7's note above
-- says to believe THIS check rather than the text comparison when the two
-- disagree. The privileges are identical; only the representation changed, and
-- there is no SQL that puts relacl back to NULL.
--
-- FOR PRODUCTION, none of the above runs. Use scripts/sql/snapshot_view_acls.sql
-- instead: it is read-only, it is taken BEFORE and AFTER the real 014, and the
-- diff recipe in its header is the production evidence that the ACL survived.

-- ===========================================================================
-- R — the reporting acceptance queries. These are the ones to paste into a
--     review: they prove the counts and the averages describe the same rows.
-- ===========================================================================

-- R1 . THE COUNTS QUERY. Total vs scored vs ungradeable vs unscoreable vs
--      contract_failed, over the whole table -- and the reconciliation that
--      says nothing fell between the buckets.
SELECT
  count(*)                                                        AS total_rows,
  count(*) FILTER (WHERE eval_score_is_usable(contract_status, gradeable, final_score))
                                                                  AS scored,
  count(*) FILTER (WHERE contract_status = 'ungradeable')         AS ungradeable,
  count(*) FILTER (WHERE contract_status = 'unscoreable')         AS unscoreable,
  count(*) FILTER (WHERE contract_status = 'contract_failed')     AS contract_failed,
  count(*) FILTER (WHERE contract_status = 'ok'
                     AND NOT eval_score_is_usable(contract_status, gradeable, final_score))
                                                                  AS ok_without_score,
  -- The buckets partition the table. This column must be true.
  count(*) = count(*) FILTER (WHERE eval_score_is_usable(contract_status, gradeable, final_score))
           + count(*) FILTER (WHERE contract_status = 'ungradeable')
           + count(*) FILTER (WHERE contract_status = 'unscoreable')
           + count(*) FILTER (WHERE contract_status = 'contract_failed')
           + count(*) FILTER (WHERE contract_status = 'ok'
                                AND NOT eval_score_is_usable(contract_status, gradeable, final_score))
                                                                  AS buckets_partition_total
  FROM agent_evaluations;
-- `contract_failed` must be 0: workflow 02 routes that status to
-- 'Mark judge failed' and writes no row. A non-zero value means some other
-- writer stored a score the scoring engine refused to stand behind.
--
-- `unscoreable` should be NON-ZERO within a few days of rollout. It is written
-- by 'Store unscoreable outcome' (the worker refused before pass 1) and by
-- 'Store evaluation' (a pass-2 refusal beside a successful pass 1). Zero after
-- a week, on a pipeline whose queue shows dead-lettered unscoreable jobs, means
-- the store node is not running -- check R10.

-- R2 . the same reconciliation, per scorecard row. Every row must reconcile.
--
--      amber_shadow_count is DELIBERATELY NOT IN THE SUM. The five buckets
--      partition evaluated_interactions, and a shadowed row was never in
--      evaluated_interactions -- adding it to the identity would make every
--      row with a bad-ASR call read `reconciles = false`. It is selected here
--      beside them so the reader can see how big the population outside the
--      partition is.
SELECT agent_id, full_name, prompt_version, rubric_version, model, model_fingerprint,
       evaluated_interactions, scored_interactions, ungradeable_count,
       unscoreable_count, contract_failed_count, ok_without_score_count,
       amber_shadow_count, amber_shadow_usable_count,
       evaluated_interactions = scored_interactions + ungradeable_count
                              + unscoreable_count + contract_failed_count
                              + ok_without_score_count AS reconciles
  FROM v_agent_scorecard
 ORDER BY reconciles, amber_shadow_count DESC, evaluated_interactions DESC;
-- expect `reconciles` true on every row -- including rows where
-- evaluated_interactions is 0 because every one of the group's evaluations was
-- shadowed (0 = 0+0+0+0+0). Sort puts any false first, then the biggest
-- shadow populations.

-- R2b . and the scorecard's own totals against the like-for-like preflight.
SELECT sum(evaluated_interactions)  AS scorecard_visible_total,
       sum(ok_without_score_count)  AS ok_without_score_total,
       sum(amber_shadow_count)      AS asr_shadowed_total
  FROM v_agent_scorecard;
-- `scorecard_visible_total` must equal P2b's `scorecard_visible_total`,
-- `ok_without_score_total` must equal P2b's `without_score`, and
-- `asr_shadowed_total` must equal P2b's `asr_shadowed`, immediately after 014.
-- None of them may be compared to P2, which counts the whole table and applies
-- neither the agent filter nor the D1 rule.

-- R2c . SOL'S D1 RULE, PRICED. How much of each agent's month the amber rule
--       removed, and -- the column to actually look at -- whether what is left
--       is still enough to publish anything. A row with a large
--       `shadow_share_pct` and `n_usable` under 30 is an agent about whom this
--       system now says nothing, and the reason is the phone line, not them.
SELECT full_name, prompt_version, model, model_fingerprint,
       evaluated_interactions,
       amber_shadow_count,
       amber_shadow_usable_count,
       n_usable,
       round(100.0 * amber_shadow_count
             / nullif(evaluated_interactions + amber_shadow_count, 0), 1)
                                                       AS shadow_share_pct,
       band_stable AS publishable
  FROM v_agent_scorecard
 WHERE amber_shadow_count > 0
 ORDER BY amber_shadow_count DESC;
-- `amber_shadow_usable_count` is the number that would join n_usable if the
-- audio were re-transcribed and came back green: it is the size of the prize
-- for fixing the ASR, per agent, and it is the honest input to the "should we
-- re-transcribe the backlog" conversation.

-- R2d . THE GROUPS THAT ONLY EXIST IN SHADOW. An agent (or a version
--       co-ordinate) whose every evaluation was shadowed still gets a row --
--       the view takes its group universe before the D1 filter, precisely so
--       this list is possible. Without that these rows would have vanished from
--       the scorecard with no trace, which is the exact failure mode 014 exists
--       to stop.
SELECT full_name, prompt_version, rubric_version, model, model_fingerprint,
       evaluated_interactions, amber_shadow_count
  FROM v_agent_scorecard
 WHERE evaluated_interactions = 0
   AND amber_shadow_count > 0
 ORDER BY amber_shadow_count DESC;
-- Not an error. Every row here is a person about whose month the reporting
-- layer is now, correctly, silent -- and a call recording worth re-running.

-- R3 . n_usable and scored_interactions are the same number, by construction.
--      They are two columns because one is a reporting count and one is the N
--      the interval is computed from; if they ever diverge, the view is wrong.
SELECT count(*) AS must_be_zero
  FROM v_agent_scorecard
 WHERE n_usable <> scored_interactions;
-- expect 0

-- R4 . the band boundaries in SQL match the ones in
--      services/worker/app/evaluate/scoring.py performance_level().
SELECT s AS score, eval_performance_band(s) AS band
  FROM unnest(ARRAY[100, 85, 84.9, 70, 69.9, 55, 54.9, 0]::numeric[]) AS s;
-- expect Excellent, Excellent, Good, Good, Average, Average,
--        Below Average, Below Average

-- R5 . THE PUBLICATION GATE. What is publishable today, and what is not,
--      with the reason. Nothing outside `publishable = true` may appear on a
--      dashboard, in a coaching conversation, or in a comparison.
SELECT full_name,
       prompt_version, rubric_version, model, model_fingerprint,
       n_usable, avg_score,
       noise_variance, score_sample_variance,
       noise_floor_half_width, ci95_half_width,
       score_ci_low, score_ci_high,
       band, band_stable AS publishable,
       CASE
         WHEN noise_variance IS NULL
           THEN 'no measured noise floor for this version co-ordinate -- re-run '
                || 'the A/A on it and INSERT the variance'
         WHEN n_usable < eval_report_param('min_n_publish')
           THEN 'too few usable scores (need '
                || eval_report_param('min_n_publish')::text || ')'
         WHEN NOT band_stable
           THEN 'interval crosses a band boundary'
         ELSE 'publishable'
       END AS gate
  FROM v_agent_scorecard
 ORDER BY band_stable DESC, n_usable DESC;
-- IMMEDIATELY AFTER ROLLOUT, EXPECT EVERY ROW TO READ
-- 'no measured noise floor for this version co-ordinate'. The only measured
-- co-ordinate is pass2-agent-quality-v3 / 1.0.0 / deepseek-chat, and the worker
-- now writes the current pass-2 prompt / deepseek-v4-flash. That is the intended,
-- fail-closed behaviour, not a broken view.

-- R5b . the gap between the floor and the complete interval, per row. This is
--       the round-4 finding made visible: publishing on the floor alone would
--       have understated every one of these.
SELECT full_name, prompt_version, model, n_usable,
       noise_floor_half_width,
       ci95_half_width,
       round(ci95_half_width - noise_floor_half_width, 2) AS understatement,
       band_stable
  FROM v_agent_scorecard
 WHERE ci95_half_width IS NOT NULL
 ORDER BY understatement DESC NULLS LAST;
-- `understatement` is how much wider the honest interval is than the noise
-- floor the first draft used as the gate. It is >= 0 by construction (the CI
-- takes the LARGER of the two variances); a row where it is large is a row that
-- the first draft would have published and this one will not.

-- R6 . cross-version contamination check. An agent appearing on more than one
--      version co-ordinate has NO single mean: the rows are separate
--      measurements and must never be summed or averaged together.
SELECT agent_id, full_name,
       count(*)                          AS version_rows,
       sum(n_usable)                     AS usable_across_versions,
       max(n_usable)                     AS usable_in_largest_version
  FROM v_agent_scorecard
 GROUP BY agent_id, full_name
HAVING count(*) > 1
 ORDER BY version_rows DESC;
-- Any row here is an agent whose history spans a prompt / rubric / model /
-- fingerprint change. `usable_across_versions` is NOT a valid N. Report only
-- `usable_in_largest_version`, or wait for the new version to reach 30.

-- R7 . fingerprint capture is actually happening on new rows. Run a few days
--      after rollout.
SELECT date_trunc('day', created_at)::date AS day,
       count(*)                            AS rows,
       count(model_fingerprint)            AS with_fingerprint,
       count(DISTINCT model_fingerprint)   AS distinct_fingerprints
  FROM agent_evaluations
 WHERE created_at > now() - interval '14 days'
 GROUP BY 1 ORDER BY 1;
-- with_fingerprint should equal rows for every day after the worker ships it,
-- EXCEPT for unscoreable rows: the worker refuses before any model call, so
-- there is no fingerprint to capture and NULL is the honest value.
-- distinct_fingerprints going from 1 to 2 is a BASELINE CHANGE: the scorecard
-- will start a new row, which is the intended behaviour, no mean may cross that
-- line, and the new co-ordinate has no measured noise floor until somebody
-- measures one.

-- R8 . input-quality view: the gap between rows seen and rows averaged is the
--      transcription problem, not an agent problem.
SELECT input_type, diarization, confidence_bucket,
       prompt_version, model, model_fingerprint,
       n, n_usable, amber_shadow_count, amber_shadow_usable_count,
       ungradeable_count, unscoreable_count, ok_without_score_count,
       avg_score, score_spread,
       noise_variance, score_sample_variance,
       noise_floor_half_width, ci95_half_width, band_stable
  FROM v_quality_by_input
 ORDER BY input_type, confidence_bucket;
-- This view does NOT exclude null-agent or bot rows, so its totals do not have
-- to match v_agent_scorecard's. That is deliberate: a transcription problem is
-- a transcription problem whoever handled the call.
--
-- READ THREE COLUMNS, NOT TWO, since the D1 rule landed: `n` is what the rule
-- let through, `n_usable` is what was averaged, `amber_shadow_count` is what
-- the rule removed from `n` entirely. On a bad ASR day the story is now in the
-- third column -- rows can read n = 0 with a large amber_shadow_count, and that
-- is the view working, not the view empty. Expect amber_shadow_count to be 0 on
-- every `chat` row: a chat is eligible whatever its (absent) transcript says,
-- and a non-zero value there means eval_asr_input_is_eligible() was changed.

-- R9 . THE QUEUE SIDE of `unscoreable`. Two things to prove after rollout:
--      it is terminal, and it did not spend a retry budget getting there.
SELECT last_error ~ '^unscoreable: '            AS unscoreable_row,
       status,
       count(*)                                 AS jobs,
       min(judge_attempts)                      AS min_judge_attempts,
       max(judge_attempts)                      AS max_judge_attempts,
       max(retries)                             AS max_retries
  FROM call_ingest_jobs
 WHERE last_error IS NOT NULL
 GROUP BY 1, 2
 ORDER BY 1 DESC, 2;
-- For unscoreable_row = true, expect `status` to be **dead_letter and nothing
-- else**, and max_judge_attempts to be **1** — the single attempt stamped by
-- 'Begin judge attempt' before the worker was called. Before
-- 'Nothing to evaluate?' existed these rows reached dead_letter with
-- judge_attempts = 5 and four extra /evaluate calls, one every 45 minutes,
-- against a transcript that had nothing in it.
--
-- A row here with status 'judge_failed' means the routing regressed: the
-- unscoreable response is being treated as a retryable judge fault again.
--
-- NOTE the two prefixes. `unscoreable: ` is the pass-2 refusal path, which now
-- ALSO writes an evaluation row (R10). `asr_quality_red: ` is the ASR gate,
-- which writes a transcript but no evaluation -- there was never a pass 2 to
-- record. They share this node and they are not the same outcome.

-- R10 . `unscoreable` LANDS IN TWO PLACES AND THEY MUST AGREE (rewritten for
--       round 4, which reversed "write no row"). Every job dead-lettered with
--       an `unscoreable: ` reason must now have exactly one evaluation row
--       carrying contract_status = 'unscoreable'.
SELECT count(*) FILTER (WHERE j.uniqueid IS NOT NULL AND e.interaction_id IS NOT NULL)
                                                    AS job_and_row_expect_all,
       count(*) FILTER (WHERE j.uniqueid IS NOT NULL AND e.interaction_id IS NULL)
                                                    AS job_without_row_expect_zero,
       count(*) FILTER (WHERE j.uniqueid IS NOT NULL AND e.interaction_id IS NOT NULL
                          AND e.contract_status <> 'unscoreable')
                                                    AS row_with_wrong_status_expect_zero
  FROM call_ingest_jobs j
  LEFT JOIN agent_evaluations e ON e.interaction_id = j.interaction_id
 WHERE j.status = 'dead_letter'
   AND j.last_error ~ '^unscoreable: ';
-- `job_without_row_expect_zero` must be 0 after rollout. A non-zero value means
-- 'Store unscoreable outcome' did not run or returned zero rows and the job was
-- terminalised anyway -- which the 'Unscoreable stored?' gate is there to make
-- impossible, so investigate the gate before anything else.
--
-- Rows written before this revision shipped are the legitimate exception: they
-- were dead-lettered when the policy was "no row". Bound the query by
-- `j.updated_at > <rollout timestamp>` to separate the two.

-- R10b . the other direction: an `unscoreable` evaluation row whose job is NOT
--        dead-lettered. That is the pass-1-succeeded variant, stored by
--        'Store evaluation' via 'Pass 2 usable?', and it is a legitimate and
--        different outcome: the call WAS evaluated, pass 2 simply found nothing
--        to grade. It counts in unscoreable_count exactly once, like the other.
SELECT j.status                       AS job_status,
       count(*)                       AS unscoreable_rows
  FROM agent_evaluations e
  LEFT JOIN call_ingest_jobs j ON j.interaction_id = e.interaction_id
 WHERE e.contract_status = 'unscoreable'
 GROUP BY 1
 ORDER BY 2 DESC;
-- Expect 'dead_letter' (the pre-pass-1 refusal) and possibly 'evaluated' (the
-- pass-1-succeeded variant, which current worker control flow cannot normally
-- produce). Anything in 'judge_failed' is a routing regression.

-- R11 . unscoreable rows carry no score-shaped data. `Store unscoreable
--       outcome' writes nulls and empty containers on purpose; a module score
--       sitting beside a null final_score is a row that is half one run and
--       half another.
SELECT count(*) AS must_be_zero
  FROM agent_evaluations
 WHERE contract_status = 'unscoreable'
   AND (final_score IS NOT NULL
     OR performance_level IS NOT NULL
     OR gradeable
     OR m1_reception IS NOT NULL OR m2_offer     IS NOT NULL
     OR m3_objections IS NOT NULL OR m4_followup IS NOT NULL
     OR m5_closing IS NOT NULL
     OR breakdown  <> '{}'::jsonb
     OR evidence   <> '[]'::jsonb
     OR notes IS NULL);
-- expect 0. `notes IS NULL` is in the list because the reason is the whole
-- point of storing the row: an unscoreable evaluation that does not say why is
-- no better than the missing row it replaced.

-- ===========================================================================
-- F1 — THE UNSCOREABLE FIXTURE, AS SQL INTEGRATION (round-5 review finding
--      F16).  MUTATES -- own fixture, COMMITTED
--
--      NOT END-TO-END, AND THE LABEL MATTERS. This drives the four statements
--      workflow 02 sends -- 02_claim_work, 02_begin_judge_attempt,
--      02_store_unscoreable_outcome, 02_mark_unscoreable -- straight at the
--      database, in the order and with the parameters the workflow uses. What
--      it proves is that the SQL contract holds: one evaluation row with
--      contract_status = 'unscoreable', a terminal dead-lettered job, one
--      judge attempt, no retry, and the row counted but never averaged.
--
--      What it does NOT prove is that n8n sends those statements, in that
--      order, with those parameters -- the node wiring, the IF branches, the
--      expression that builds the reason string and the credential the nodes
--      run under are all outside it. A refactor of workflow 02 that stops
--      calling `Store unscoreable outcome` altogether would leave every
--      assertion below passing. THE TRUE END-TO-END BELONGS IN G2 (the n8n
--      trial run against a real recording), and until G2 has run, "unscoreable
--      works" is a claim about SQL only.
--      through the workflow, with a mandatory cleanup. Runbook step 4.
--
--      This is the only test that exercises the whole reversed decision: worker
--      refusal -> stored row -> terminal job -> no retry. It needs the workflow
--      running against a staging copy, because the write it proves is made by
--      an n8n node and not by this file.
-- ===========================================================================
--
-- SET UP. A call whose transcript is under MIN_SCOREABLE_CHARS (100 normalised
-- characters of speech, services/worker/app/main.py). Nine words of greeting
-- and nothing else is the realistic shape -- the customer hung up.
--
-- 1. Register a job for a fixture recording and let the workflow transcribe it,
--    or insert the interaction + transcript directly and set the job to
--    'transcribed' so the claim routes it to the judge path:
--
--    INSERT INTO call_ingest_jobs (uniqueid, filename, audio_uri, meta, status,
--                                  interaction_id)
--    VALUES ('ACC-F1', 'f1.wav', 'drive://f1', '{"uniqueid":"ACC-F1"}'::jsonb,
--            'transcribed', '<interaction uuid>');
--    -- the transcript's full_text must normalise to < 100 characters, and
--    -- asr_metrics.asr_quality_status must be 'green' so the ASR gates pass it
--    -- through to 'Begin judge attempt' rather than dead-lettering it first.
--
-- 2. Run one execution of workflow 02.
--
-- THE FOUR THINGS THAT MUST ALL BE TRUE. Any one of them false is a no-go.
--
-- (a) ONE stored status outcome, with the reason:
SELECT e.contract_status, e.gradeable, e.final_score, e.prompt_version,
       e.rubric_version, e.model, left(e.notes, 120) AS reason
  FROM agent_evaluations e
  JOIN call_ingest_jobs j ON j.interaction_id = e.interaction_id
 WHERE j.uniqueid = 'ACC-F1';
-- expect exactly one row: 'unscoreable', false, NULL, the worker's pass-2
-- prompt version, the rubric version, 'none (refused before any model call)',
-- and a reason naming the character count and the minimum.
--
-- (b) TERMINAL JOB, ONE judge attempt, no retry:
SELECT status, judge_attempts, asr_attempts, retries,
       claim_token IS NULL AS lease_cleared, left(last_error, 80)
  FROM call_ingest_jobs WHERE uniqueid = 'ACC-F1';
-- expect 'dead_letter', judge_attempts = 1, retries unchanged, lease_cleared
-- true, last_error starting 'unscoreable: '.
--
-- (c) THE ROW IS COUNTED, not averaged:
SELECT unscoreable_count, scored_interactions, n_usable, avg_score, band_stable
  FROM v_agent_scorecard
 WHERE agent_id = (SELECT agent_id FROM agent_evaluations e
                     JOIN call_ingest_jobs j ON j.interaction_id = e.interaction_id
                    WHERE j.uniqueid = 'ACC-F1');
-- unscoreable_count must have gone UP by one; n_usable and avg_score must be
-- unchanged. (If the fixture agent has no other evaluations, expect
-- unscoreable_count 1, n_usable 0, avg_score NULL, band_stable false.)
--
-- (d) NO SECOND ATTEMPT. Run a second execution 45+ minutes later, or force one
--     from n8n, and re-run (b). judge_attempts must still be 1 and status must
--     still be 'dead_letter': a dead-lettered row is not claimable, and nothing
--     re-opens it.
--
-- CLEAN UP. MANDATORY -- this fixture is committed.
-- DELETE FROM alert_occurrences WHERE interaction_id IN
--   (SELECT interaction_id FROM call_ingest_jobs WHERE uniqueid = 'ACC-F1');
-- DELETE FROM agent_evaluations WHERE interaction_id IN
--   (SELECT interaction_id FROM call_ingest_jobs WHERE uniqueid = 'ACC-F1');
-- DELETE FROM interaction_analysis WHERE interaction_id IN
--   (SELECT interaction_id FROM call_ingest_jobs WHERE uniqueid = 'ACC-F1');
-- DELETE FROM transcripts WHERE interaction_id IN
--   (SELECT interaction_id FROM call_ingest_jobs WHERE uniqueid = 'ACC-F1');
-- DELETE FROM call_ingest_jobs WHERE uniqueid = 'ACC-F1';
-- -- interactions last: the job references it.
-- SELECT count(*) AS must_be_zero FROM call_ingest_jobs WHERE uniqueid = 'ACC-F1';

-- ===========================================================================
-- P5 — MUTATES. Proves the four statuses round-trip and are counted correctly.
--      Creates its own fixture rows and ROLLS BACK. Safe on production only in
--      the sense that it commits nothing; still prefer staging.
-- ===========================================================================
-- BEGIN;
--
-- -- four fixture evaluations against four throwaway interactions. Substitute
-- -- real interaction_ids from a test agent, or create interactions first --
-- -- agent_evaluations.interaction_id is a FK and is UNIQUE.
-- INSERT INTO agent_evaluations
--   (interaction_id, schema_version, prompt_version, rubric_version, model,
--    input_type, final_score, contract_status, gradeable, model_fingerprint)
-- VALUES
--   ('<uuid-1>', '1.0', 'p-test', 'r-test', 'm-test', 'call_transcript',
--    80,   'ok',              true,  'fp_test'),
--   ('<uuid-2>', '1.0', 'p-test', 'r-test', 'm-test', 'call_transcript',
--    NULL, 'ungradeable',     false, 'fp_test'),
--   ('<uuid-3>', '1.0', 'p-test', 'r-test', 'm-test', 'call_transcript',
--    NULL, 'unscoreable',     false, 'fp_test'),
--   ('<uuid-4>', '1.0', 'p-test', 'r-test', 'm-test', 'call_transcript',
--    NULL, 'contract_failed', false, 'fp_test');
--
-- -- expect: total 4, scored 1, ungradeable 1, unscoreable 1, contract_failed 1,
-- --         ok_without_score 0, avg over the single usable row = 80,
-- --         noise_variance NULL (nobody measured 'p-test'), ci95_half_width
-- --         NULL, band_stable false
-- SELECT evaluated_interactions, scored_interactions, ungradeable_count,
--        unscoreable_count, contract_failed_count, ok_without_score_count,
--        n_usable, avg_score, noise_variance, ci95_half_width, band, band_stable
--   FROM v_agent_scorecard
--  WHERE prompt_version = 'p-test' AND model_fingerprint = 'fp_test';
--
-- -- Now give 'p-test' a measured noise floor and watch the gate change its
-- -- mind. This is the versioning working: the value answers for this
-- -- co-ordinate and for no other.
-- INSERT INTO eval_noise_params (param_key, prompt_version, rubric_version,
--                                model, model_fingerprint, value, source)
-- VALUES ('repeat_run_variance', 'p-test', 'r-test', 'm-test', NULL,
--         188.70, 'P5 fixture, rolled back');
-- SELECT n_usable, ci95_half_width, band_stable   -- ci95 now a number;
--   FROM v_agent_scorecard                        -- band_stable still false,
--  WHERE prompt_version = 'p-test';               -- because n_usable = 1 < 30
--
-- -- the CHECK constraint refuses anything else
-- -- INSERT ... contract_status = 'probably_fine';   -- must raise
--
-- ROLLBACK;

-- ===========================================================================
-- P6 — SOL'S D1 RULE, PROVED.  MUTATES, AND CLEANS ITSELF UP: the whole
--      section runs inside BEGIN ... ROLLBACK, so it commits nothing. Unlike
--      P5 it is written to be RUN AS IT STANDS rather than pasted and edited,
--      because the thing it proves is a population rule and a population rule
--      is only proved by rows.
--
--      IT DEPENDS ON NO PRODUCTION DATA. It creates its own agent, its own four
--      interactions, its own three transcripts and its own four evaluations,
--      all under fixed fixture UUIDs, and asserts against those UUIDs only.
--      Run it on staging; it is safe on production in the sense that it commits
--      nothing, and there is still no reason to.
--
--      THE FOUR CASES, one per line of the brief:
--        D1-CHAT   a chat evaluation, no transcript at all      -> PRESENT
--        D1-GREEN  a call whose asr_quality_status is 'green'   -> PRESENT
--        D1-AMBER  a call whose asr_quality_status is 'amber'   -> ABSENT
--        D1-NOMET  a call whose transcript has no asr_metrics   -> ABSENT
--
--      All four carry a usable score (contract_status 'ok', gradeable, a
--      number), so nothing here is excluded by eval_score_is_usable(). The ONLY
--      thing separating them is the D1 rule, which is the point: a fixture that
--      passes for the wrong reason passes for all four.
-- ===========================================================================
BEGIN;

INSERT INTO agents (agent_id, full_name, team, is_bot, bitrix_user_id)
VALUES ('d1000000-0000-4000-8000-00000000a9e7', 'ACC-D1 fixture agent',
        'ACC-D1', false, 'acc-d1-fixture');

INSERT INTO interactions (interaction_id, agent_id, channel, external_id,
                          external_source, started_at)
VALUES
  ('d1000000-0000-4000-8000-000000000001',
   'd1000000-0000-4000-8000-00000000a9e7', 'whatsapp',
   'ACC-D1-CHAT',  'acc_d1_fixture', now()),
  ('d1000000-0000-4000-8000-000000000002',
   'd1000000-0000-4000-8000-00000000a9e7', 'phone_call',
   'ACC-D1-GREEN', 'acc_d1_fixture', now()),
  ('d1000000-0000-4000-8000-000000000003',
   'd1000000-0000-4000-8000-00000000a9e7', 'phone_call',
   'ACC-D1-AMBER', 'acc_d1_fixture', now()),
  ('d1000000-0000-4000-8000-000000000004',
   'd1000000-0000-4000-8000-00000000a9e7', 'phone_call',
   'ACC-D1-NOMET', 'acc_d1_fixture', now());

-- Three transcripts. D1-CHAT gets none: a chat has no transcript row at all,
-- and that is exactly the case the LEFT JOIN has to keep.
INSERT INTO transcripts (interaction_id, audio_uri, asr_provider,
                         asr_model_version, asr_confidence, full_text,
                         diarization, asr_metrics)
VALUES
  ('d1000000-0000-4000-8000-000000000002', 'drive://acc-d1-green',
   'acc-d1-fixture', 'fixture', 0.90, 'green fixture transcript',
   'dual_channel', '{"asr_quality_status": "green"}'::jsonb),
  ('d1000000-0000-4000-8000-000000000003', 'drive://acc-d1-amber',
   'acc-d1-fixture', 'fixture', 0.90, 'amber fixture transcript',
   'dual_channel', '{"asr_quality_status": "amber"}'::jsonb),
  -- No asr_quality_status key at all. The column's own DEFAULT is '{}', so this
  -- is not a contrived case: it is every transcript written before 010.
  ('d1000000-0000-4000-8000-000000000004', 'drive://acc-d1-nomet',
   'acc-d1-fixture', 'fixture', 0.90, 'no-metrics fixture transcript',
   'dual_channel', '{}'::jsonb);

-- Four evaluations, all four USABLE, on one shared version co-ordinate so they
-- land in one scorecard row and the counts below are unambiguous.
INSERT INTO agent_evaluations
  (interaction_id, agent_id, schema_version, prompt_version, rubric_version,
   model, model_fingerprint, input_type, final_score, contract_status,
   gradeable, m5_closing)
VALUES
  ('d1000000-0000-4000-8000-000000000001',
   'd1000000-0000-4000-8000-00000000a9e7', '1.0', 'p-acc-d1', 'r-acc-d1',
   'm-acc-d1', 'fp-acc-d1', 'chat',            60, 'ok', true, 60),
  ('d1000000-0000-4000-8000-000000000002',
   'd1000000-0000-4000-8000-00000000a9e7', '1.0', 'p-acc-d1', 'r-acc-d1',
   'm-acc-d1', 'fp-acc-d1', 'call_transcript', 80, 'ok', true, 80),
  ('d1000000-0000-4000-8000-000000000003',
   'd1000000-0000-4000-8000-00000000a9e7', '1.0', 'p-acc-d1', 'r-acc-d1',
   'm-acc-d1', 'fp-acc-d1', 'call_transcript', 40, 'ok', true, 40),
  ('d1000000-0000-4000-8000-000000000004',
   'd1000000-0000-4000-8000-00000000a9e7', '1.0', 'p-acc-d1', 'r-acc-d1',
   'm-acc-d1', 'fp-acc-d1', 'call_transcript', 20, 'ok', true, 20);

-- (a) THE POPULATION, ROW BY ROW. This is the assertion the brief asks for, and
--     it names each fixture so a failure says WHICH case broke.
SELECT i.external_id,
       e.input_type,
       t.asr_metrics->>'asr_quality_status'                     AS asr_status,
       eval_score_is_usable(e.contract_status, e.gradeable, e.final_score)
                                                                AS usable,
       eval_asr_input_is_eligible(e.input_type,
                                  t.asr_metrics->>'asr_quality_status')
                                                                AS d1_eligible,
       (v.evaluation_id IS NOT NULL)                            AS in_usable_view,
       CASE i.external_id
         WHEN 'ACC-D1-CHAT'  THEN true
         WHEN 'ACC-D1-GREEN' THEN true
         ELSE                     false
       END = (v.evaluation_id IS NOT NULL)                      AS as_specified
  FROM agent_evaluations e
  JOIN interactions i              ON i.interaction_id = e.interaction_id
  LEFT JOIN transcripts t          ON t.interaction_id = e.interaction_id
  LEFT JOIN v_usable_evaluations v ON v.evaluation_id  = e.evaluation_id
 WHERE i.external_source = 'acc_d1_fixture'
 ORDER BY i.external_id;
-- expect four rows, `usable` TRUE on all four, and:
--   ACC-D1-AMBER  call_transcript  amber   d1_eligible false  in_usable_view false
--   ACC-D1-CHAT   chat             (null)  d1_eligible true   in_usable_view true
--   ACC-D1-GREEN  call_transcript  green   d1_eligible true   in_usable_view true
--   ACC-D1-NOMET  call_transcript  (null)  d1_eligible false  in_usable_view false
-- `as_specified` must be TRUE on every row. `usable` true on all four is what
-- makes this a test of the D1 rule and not of eval_score_is_usable().

-- (b) THE SCORECARD. One row, and every number in it is green-and-chat only.
SELECT evaluated_interactions, calls, chats,
       scored_interactions, n_usable,
       amber_shadow_count, amber_shadow_usable_count,
       avg_score, band_stable
  FROM v_agent_scorecard
 WHERE agent_id = 'd1000000-0000-4000-8000-00000000a9e7';
-- expect exactly one row:
--   evaluated_interactions    2   (the chat and the green call)
--   calls                     1   chats 1
--   scored_interactions       2   n_usable 2
--   amber_shadow_count        2   (amber + no-metrics)
--   amber_shadow_usable_count 2   (both WOULD have been averaged)
--   avg_score              70.0   = mean(60, 80)
--   band_stable           false   (n_usable 2 < 30, and 'p-acc-d1' has no
--                                  measured noise floor either)
--
-- 70.0 IS THE WHOLE POINT. Without the D1 rule this row would read
-- evaluated_interactions 4, n_usable 4 and avg_score 50.0 -- twenty points
-- lower, more than a band, and entirely an artefact of the phone line.

-- (c) THE BUCKET PARTITION STILL HOLDS, with the shadow count outside it.
SELECT evaluated_interactions,
       scored_interactions + ungradeable_count + unscoreable_count
         + contract_failed_count + ok_without_score_count       AS bucket_sum,
       evaluated_interactions = scored_interactions + ungradeable_count
                              + unscoreable_count + contract_failed_count
                              + ok_without_score_count          AS reconciles,
       amber_shadow_count
  FROM v_agent_scorecard
 WHERE agent_id = 'd1000000-0000-4000-8000-00000000a9e7';
-- expect 2, 2, true, 2. The shadowed rows are NOT on either side of the
-- identity; adding them to it is the mistake this comment exists to prevent.

-- (d) THE INPUT-QUALITY VIEW tells the same story from the other side, and
--     keeps the bad-ASR rows visible in amber_shadow_count rather than losing
--     them.
SELECT input_type, diarization, n, n_usable, amber_shadow_count, avg_score
  FROM v_quality_by_input
 WHERE prompt_version = 'p-acc-d1'
 ORDER BY input_type, diarization NULLS FIRST;
-- expect two rows, one per input_type:
--   chat             (null diarization)  n 1  n_usable 1  amber_shadow 0  60.0
--   call_transcript  dual_channel        n 1  n_usable 1  amber_shadow 2  80.0
-- The amber and no-metrics calls share the green call's diarization and
-- confidence bucket, so they land on the SAME row as amber_shadow_count -- that
-- is what keeps this view usable as an ASR diagnostic after the rule.

-- (e) THE ROWS ARE STILL THERE. Shadow-only means excluded from the reporting
--     layer, NOT deleted: shadow analysis reads agent_evaluations.
SELECT count(*) AS must_be_4
  FROM agent_evaluations e
  JOIN interactions i ON i.interaction_id = e.interaction_id
 WHERE i.external_source = 'acc_d1_fixture';
-- expect 4

-- (f) and the shadow comparison the rule is FOR: do amber calls score
--     differently from green ones? On real data this is the query that decides
--     whether the rule can ever be relaxed.
SELECT coalesce(t.asr_metrics->>'asr_quality_status', '(none)') AS asr_status,
       count(*)             AS n,
       avg(e.final_score)   AS mean_score
  FROM agent_evaluations e
  JOIN interactions i     ON i.interaction_id = e.interaction_id
  LEFT JOIN transcripts t ON t.interaction_id = e.interaction_id
 WHERE i.external_source = 'acc_d1_fixture'
   AND e.input_type = 'call_transcript'
 GROUP BY 1 ORDER BY 1;
-- expect (none) 1 / 20, amber 1 / 40, green 1 / 80 on the fixture.

ROLLBACK;
-- SELF-CLEANING. Nothing above is committed, so there is no cleanup step to
-- forget. If you deliberately change ROLLBACK to COMMIT in order to poke at the
-- rows, undo it in this order -- agent_evaluations, transcripts, interactions,
-- agents -- and confirm with:
--   SELECT count(*) AS must_be_zero FROM interactions
--    WHERE external_source = 'acc_d1_fixture';
