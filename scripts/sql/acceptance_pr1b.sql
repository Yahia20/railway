-- Verification and acceptance queries for PR1B (the score-free follow-up queue).
-- Every SQL statement quoted in docs/PR1B-alerts.md lives here so it can be
-- reviewed and run without copying out of prose.
--
-- NOTHING in this file has been run against any database.
--
-- There is no delivery test in here any more, because there is no delivery:
-- recordings reach us about a day after the call, so the pipeline fills a queue
-- (`v_alert_queue`, `v_alert_digest_daily`) and stops. Nothing POSTs anywhere.

-- ---------------------------------------------------------------------------
-- After applying db/migrations/013_alert_rules.sql
-- ---------------------------------------------------------------------------

-- 1 . the catalogue seeded, with the seeded state each rule is SUPPOSED to be in
SELECT rule_code, rule_version, is_alert, active, params
  FROM alert_rules
 ORDER BY rule_code;
-- expect exactly four rules:
--   complaint_or_cancellation  is_alert = FALSE (triage: no quote behind it)
--                              active   = true
--   hot_real_ask_promised      is_alert = true,  active = true
--   hot_real_ask_uncommitted   is_alert = true,  active = true
--   promise_open_or_overdue    is_alert = FALSE, active = FALSE
--                              (nothing materialises follow_ups and no sweep
--                               calls the function -- see docs/PR1B-alerts.md)

-- 2 . SPELLING CHECK for params keys. THIS IS NOT A PROOF -- read the note.
--
--     A tunable that nothing reads is worse than no tunable: somebody will turn
--     it and believe the rule changed. That is the property we care about, and
--     THIS QUERY DOES NOT TEST IT. The list below is a hand-maintained copy of
--     what the function is *supposed* to read; it is not derived from the
--     function, so it catches a typo'd or orphaned key and nothing else. If
--     every branch stopped consulting `params` tomorrow, this query would still
--     return zero rows and still look green.
--
--     The actual proof is section P below: one block per key, each of which
--     flips the key on a fixture and asserts the ANSWER CHANGES. That is the
--     only shape of test that can tell a real parameter from a decorative one,
--     and it is how the two decorative parameters were eventually caught.
--
--     Keeping both is deliberate: this one is cheap and runs against the live
--     catalogue, P is thorough and needs a fixture. Neither substitutes for the
--     other. When you add a key, add it HERE and add a block to P.
SELECT rule_code, param_key
  FROM alert_rules, LATERAL jsonb_object_keys(params) AS param_key
 WHERE param_key NOT IN ('lead_temperatures', 'require_real_ask_quote_valid',
                   'require_valid_promise', 'require_asr_quality', 'intents',
                   'due_within_hours', 'include_overdue', 'include_undated')
 ORDER BY 1, 2;
-- expect ZERO rows -- meaning "no key is misspelled or orphaned", nothing more

-- 3 . the function, the queue view and the digest view exist
SELECT p.proname, pg_get_function_result(p.oid) AS returns
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE p.proname = 'evaluate_alert_rules'
   AND n.nspname = current_schema();
-- expect one row returning SETOF alert_occurrences

SELECT * FROM v_alert_queue        LIMIT 1;   -- must not error; empty is fine
SELECT * FROM v_alert_digest_daily LIMIT 1;   -- must not error; empty is fine

-- 4 . the queue state vocabulary is the new one
SELECT pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conrelid = 'alert_occurrences'::regclass
   AND contype = 'c';
-- expect: CHECK (delivery_status IN ('pending','acknowledged','suppressed'))
-- 'sent' and 'failed' are gone. They described a push that does not exist.

-- ---------------------------------------------------------------------------
-- DETERMINISTIC FIXTURE.
--
-- The first version of this file evaluated "the newest row in v_real_asks" and
-- asserted "first call > 0, second call = 0". That passes with 0 then 0 when
-- the chosen row happens to trip no rule -- i.e. it passes when the rules are
-- completely broken. Every behavioural test below therefore builds its own
-- interaction, with facts chosen so that exactly one named rule must fire, and
-- rolls the whole thing back.
--
-- Run each block on its own. They all end in ROLLBACK.
-- ---------------------------------------------------------------------------

-- Reusable setup, pasted at the top of each block below.
-- BEGIN;
--   INSERT INTO interactions (interaction_id, external_source, external_id,
--                             channel, started_at, customer_phone_e164,
--                             handled_by)
--   VALUES ('11111111-1111-1111-1111-111111111111', 'acceptance', 'ACC-B',
--           'phone_call', now() - interval '1 day', '+966500000001', 'agent');
--
--   INSERT INTO transcripts (interaction_id, audio_uri, asr_provider,
--                            asr_model_version, full_text, segments, asr_metrics)
--   VALUES ('11111111-1111-1111-1111-111111111111', 'drive://acc-b', 'acceptance',
--           'test', 'nass', '[]'::jsonb,
--           '{"asr_quality_status": "green"}'::jsonb);
--
--   INSERT INTO interaction_analysis (interaction_id, schema_version,
--                                     prompt_version, model, input_type,
--                                     raw_response)
--   VALUES ('11111111-1111-1111-1111-111111111111', '1.0', 'acc', 'acc',
--           'call_transcript',
--           '{"intent": "price_inquiry",
--             "summary_ar": "طلب عرض سعر",
--             "real_ask": {"is_real_inquiry": true,
--                          "products": ["umrah_package"],
--                          "evidence": [{"quote": "ابغى عرض عمرة"}]},
--             "commercial": {"lead_temperature": "hot"},
--             "promises_made_by_agent": [{"promise": "اتواصل معك"}],
--             "pass1_validation": {"real_ask_quote_valid": true,
--                                  "promises": [{"index": 0, "quote_valid": true}]}
--            }'::jsonb);

-- ---------------------------------------------------------------------------
-- B1 . a rule fires exactly once -- and it definitely fires the first time.
-- ---------------------------------------------------------------------------
-- BEGIN;
--   <fixture above>
--   SELECT rule_code, delivery_status
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- MUST contain exactly one row: hot_real_ask_promised / pending.
--   -- If this returns zero rows the test has FAILED, not passed.
--   SELECT count(*) AS second_call_must_be_zero
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- 0
--   SELECT count(*) AS occurrences_must_be_one FROM alert_occurrences
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   -- 1
-- ROLLBACK;

-- ---------------------------------------------------------------------------
-- B2 . changing the FACTS re-queues.
-- ---------------------------------------------------------------------------
-- BEGIN;
--   <fixture above>
--   SELECT occurrence_hash AS before_hash
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   UPDATE interaction_analysis
--      SET raw_response = jsonb_set(
--            raw_response, '{pass1_validation,promises}',
--            raw_response->'pass1_validation'->'promises'
--              || '[{"index": 1, "quote_valid": true}]'::jsonb)
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   SELECT rule_code, occurrence_hash AS after_hash
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- hot_real_ask_promised appears again with a DIFFERENT hash (the hash
--   -- carries the promise count), and alert_occurrences now holds two rows.
-- ROLLBACK;

-- ---------------------------------------------------------------------------
-- B3 . an unvalidated quote never reaches the queue.
--      Starts from a CLEAN occurrence table for this interaction -- otherwise
--      an existing occurrence deduplicates the second call and a rule that is
--      completely broken looks like it "correctly did not fire".
-- ---------------------------------------------------------------------------
-- BEGIN;
--   <fixture above>
--   DELETE FROM alert_occurrences
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   SELECT count(*) AS occurrences_must_start_at_zero FROM alert_occurrences
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   UPDATE interaction_analysis
--      SET raw_response = jsonb_set(raw_response,
--            '{pass1_validation,real_ask_quote_valid}', 'false'::jsonb)
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   SELECT rule_code FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- neither hot_ rule may appear
-- ROLLBACK;

-- ---------------------------------------------------------------------------
-- B4 . a red transcript does not produce an "uncommitted" alert.
--      Clean occurrence table first, same reason as B3.
-- ---------------------------------------------------------------------------
-- BEGIN;
--   <fixture above>
--   -- make it promise-free so hot_real_ask_uncommitted is the candidate
--   UPDATE interaction_analysis
--      SET raw_response = jsonb_set(
--            jsonb_set(raw_response, '{promises_made_by_agent}', '[]'::jsonb),
--            '{pass1_validation,promises}', '[]'::jsonb)
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   DELETE FROM alert_occurrences
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--
--   -- green: it MUST fire (this half is what makes the red half meaningful)
--   SELECT rule_code FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- expect hot_real_ask_uncommitted
--
--   DELETE FROM alert_occurrences
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   UPDATE transcripts
--      SET asr_metrics = jsonb_set(asr_metrics, '{asr_quality_status}', '"red"')
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   SELECT rule_code FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- hot_real_ask_uncommitted must NOT appear
-- ROLLBACK;

-- ---------------------------------------------------------------------------
-- B5 . suppression records evidence without filling the queue.
-- ---------------------------------------------------------------------------
-- BEGIN;
--   <fixture above>
--   UPDATE alert_rules SET is_alert = false WHERE rule_code = 'hot_real_ask_promised';
--   SELECT rule_code, delivery_status
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- delivery_status = 'suppressed'
--   SELECT count(*) AS suppressed_in_queue_must_be_zero
--     FROM v_alert_queue
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   -- 0
--   SELECT suppressed FROM v_alert_digest_daily
--    WHERE rule_code = 'hot_real_ask_promised'
--      AND alert_day = (now() AT TIME ZONE 'Asia/Riyadh')::date;
--   -- 1 -- the digest still counts it, which is what a dry run is for
-- ROLLBACK;

-- ---------------------------------------------------------------------------
-- B6 . the two tunable gates are real (Sol change 9).
--      With require_valid_promise = false the rule counts EVERY extracted
--      promise instead of only the quote-validated ones, so a call whose single
--      promise failed validation moves from "uncommitted" to "promised".
-- ---------------------------------------------------------------------------
-- BEGIN;
--   <fixture above>
--   -- one promise, validation says its quote is NOT verbatim
--   UPDATE interaction_analysis
--      SET raw_response = jsonb_set(raw_response, '{pass1_validation,promises}',
--            '[{"index": 0, "quote_valid": false}]'::jsonb)
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--
--   SELECT rule_code FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- default params (require_valid_promise = true): hot_real_ask_uncommitted
--
--   DELETE FROM alert_occurrences
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   UPDATE alert_rules
--      SET params = params || '{"require_valid_promise": false}'::jsonb
--    WHERE rule_code IN ('hot_real_ask_promised', 'hot_real_ask_uncommitted');
--   SELECT rule_code FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- now: hot_real_ask_promised. If the parameter were decorative, the answer
--   -- would not change -- which is exactly what it used to do.
-- ROLLBACK;

-- ---------------------------------------------------------------------------
-- B7 . complaint_or_cancellation is TRIAGE, not an alert.
-- ---------------------------------------------------------------------------
-- BEGIN;
--   <fixture above>
--   UPDATE interaction_analysis
--      SET raw_response = jsonb_set(raw_response, '{intent}', '"complaint"')
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   SELECT rule_code, delivery_status
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code = 'complaint_or_cancellation';
--   -- delivery_status MUST be 'suppressed' on a freshly seeded database:
--   -- the rule is recording evidence for a precision measurement, and must not
--   -- be putting an unquoted classification in front of a supervisor yet.
-- ROLLBACK;
--
-- Measure it before switching it on (needs a week of suppressed rows):
SELECT o.occurrence_id, o.created_at,
       o.fact_snapshot->>'intent'     AS intent,
       o.fact_snapshot->>'summary_ar' AS summary_ar,
       i.customer_phone_e164
  FROM alert_occurrences o
  JOIN interactions i ON i.interaction_id = o.interaction_id
 WHERE o.rule_code = 'complaint_or_cancellation'
 ORDER BY o.created_at DESC
 LIMIT 50;
-- Read 50 by hand. If the precision is good, flip is_alert to true. If it is
-- not, the fix is a quote in pass 1, not a lower bar here.

-- ---------------------------------------------------------------------------
-- B8 . the queue survives a pass-2 failure.
-- The workflow evaluates the rules off a CONFIRMED terminal transition -- both
-- 'evaluated' and 'judge_failed' reach "Evaluate alert rules" -- so a hot lead
-- with a verified quote is queued even when the judge failed and the job is
-- waiting for a retry.
-- ---------------------------------------------------------------------------
SELECT j.uniqueid, j.status,
       (ia.interaction_id IS NOT NULL) AS pass1_stored,
       (ae.interaction_id IS NOT NULL) AS pass2_stored,
       count(o.occurrence_id)          AS occurrences
  FROM call_ingest_jobs j
  LEFT JOIN interaction_analysis ia ON ia.interaction_id = j.interaction_id
  LEFT JOIN agent_evaluations   ae ON ae.interaction_id = j.interaction_id
  LEFT JOIN alert_occurrences    o ON o.interaction_id  = j.interaction_id
 WHERE j.status = 'judge_failed'
   AND j.interaction_id IS NOT NULL
 GROUP BY 1, 2, 3, 4
 ORDER BY j.uniqueid;
-- a contract_failed row must show pass1_stored = true, pass2_stored = false,
-- and may legitimately show occurrences > 0

-- ---------------------------------------------------------------------------
-- B9 . a stale worker cannot queue an alert.
-- The alert node is reachable only from "Job finalised?", and its own SQL fence
-- requires the job to still be in the terminal state that execution wrote, with
-- no lease on it. See acceptance_pr1a.sql, A3e.
-- ---------------------------------------------------------------------------
-- Covered in acceptance_pr1a.sql A3e -- kept cross-referenced rather than
-- duplicated, because it is a lease test that happens to be about alerting.

-- ---------------------------------------------------------------------------
-- B10 . promise_open_or_overdue is INACTIVE, on purpose, and the reasons are
--       measurable rather than folklore.
-- ---------------------------------------------------------------------------
SELECT active, is_alert FROM alert_rules WHERE rule_code = 'promise_open_or_overdue';
-- expect false, false

SELECT count(*) AS follow_ups_rows FROM follow_ups;
-- expect 0: workflow 03 materialises them and it is inactive

-- Even once 03 runs, its "Materialise promises" step requires
-- i.agent_id IS NOT NULL, and 'q' queue recordings deliberately have a null
-- agent. This is the size of that gap for calls:
SELECT count(*) FILTER (WHERE i.agent_id IS NULL)     AS promises_lost_to_null_agent,
       count(*) FILTER (WHERE i.agent_id IS NOT NULL) AS promises_materialisable
  FROM interaction_analysis ia
  JOIN interactions i ON i.interaction_id = ia.interaction_id
 CROSS JOIN LATERAL jsonb_array_elements(
       coalesce(ia.raw_response->'promises_made_by_agent', '[]'::jsonb)) AS p
 WHERE i.channel = 'phone_call';
-- Activating workflow 03 ALONE does not make this rule work. All three of
-- (03 running) + (null-agent exclusion resolved) + (a scheduled sweep that
-- calls evaluate_alert_rules after materialisation) are required.

-- ---------------------------------------------------------------------------
-- P . EVERY DECLARED params KEY IS READ.  MUTATES -- own fixture, rolled back.
--
-- Section 2 proves only that no key is misspelled. These blocks prove the
-- thing that matters: change the key, and the rule's answer changes. Each is
-- the B6 shape -- establish the baseline answer, flip one key, show a different
-- answer -- because a parameter the function ignores produces the SAME answer
-- both times, which is exactly what the two decorative gates used to do.
--
-- Every block starts from the section-B fixture and clears alert_occurrences
-- for the interaction before each call, so that ordinary deduplication can
-- never masquerade as "the rule correctly did not fire".
--
-- P3 is B6 and is not repeated here.
-- ---------------------------------------------------------------------------

-- P1 . lead_temperatures  (hot_real_ask_promised, hot_real_ask_uncommitted)
-- BEGIN;
--   <fixture above>   -- lead_temperature = 'hot', one valid promise
--   SELECT rule_code FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- baseline: hot_real_ask_promised
--   DELETE FROM alert_occurrences WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   UPDATE alert_rules SET params = params || '{"lead_temperatures": ["cold"]}'::jsonb
--    WHERE rule_code IN ('hot_real_ask_promised','hot_real_ask_uncommitted');
--   SELECT count(*) AS must_be_zero
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code LIKE 'hot_%';
--   -- 0. If the key were decorative the hot rule would fire again.
-- ROLLBACK;

-- P2 . require_real_ask_quote_valid  (both hot rules)
-- BEGIN;
--   <fixture above>
--   UPDATE interaction_analysis
--      SET raw_response = jsonb_set(raw_response,
--            '{pass1_validation,real_ask_quote_valid}', 'false'::jsonb)
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   SELECT count(*) AS must_be_zero
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code LIKE 'hot_%';
--   -- 0 with the gate on (this is B3)
--   DELETE FROM alert_occurrences WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   UPDATE alert_rules
--      SET params = params || '{"require_real_ask_quote_valid": false}'::jsonb
--    WHERE rule_code IN ('hot_real_ask_promised','hot_real_ask_uncommitted');
--   SELECT rule_code FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- hot_real_ask_promised fires on the unvalidated extraction alone.
-- ROLLBACK;

-- P3 . require_valid_promise -- see B6 above. Same fixture, same shape:
--      flipping it moves the call from hot_real_ask_uncommitted to
--      hot_real_ask_promised.

-- P4 . require_asr_quality  (hot_real_ask_uncommitted)
-- BEGIN;
--   <fixture above>
--   -- make it promise-free so the uncommitted rule is the candidate, and red
--   UPDATE interaction_analysis
--      SET raw_response = jsonb_set(
--            jsonb_set(raw_response, '{promises_made_by_agent}', '[]'::jsonb),
--            '{pass1_validation,promises}', '[]'::jsonb)
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   UPDATE transcripts
--      SET asr_metrics = jsonb_set(asr_metrics, '{asr_quality_status}', '"red"')
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   DELETE FROM alert_occurrences WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   SELECT count(*) AS must_be_zero
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code = 'hot_real_ask_uncommitted';
--   -- 0: the seeded value is 'green' and this transcript is red (this is B4)
--   DELETE FROM alert_occurrences WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   UPDATE alert_rules SET params = params || '{"require_asr_quality": "red"}'::jsonb
--    WHERE rule_code = 'hot_real_ask_uncommitted';
--   SELECT rule_code FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111');
--   -- hot_real_ask_uncommitted: the rule now demands red, and gets it.
-- ROLLBACK;

-- P5 . intents  (complaint_or_cancellation)
-- BEGIN;
--   <fixture above>
--   UPDATE interaction_analysis
--      SET raw_response = jsonb_set(raw_response, '{intent}', '"complaint"')
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   SELECT count(*) AS must_be_one
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code = 'complaint_or_cancellation';
--   -- 1 (suppressed, but recorded -- see B7)
--   DELETE FROM alert_occurrences WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   UPDATE alert_rules SET params = '{"intents": ["support"]}'::jsonb
--    WHERE rule_code = 'complaint_or_cancellation';
--   SELECT count(*) AS must_be_zero
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code = 'complaint_or_cancellation';
--   -- 0: the intent set is read, not hardcoded.
-- ROLLBACK;

-- P6/P7/P8 . due_within_hours, include_overdue, include_undated
--            (promise_open_or_overdue -- seeded INACTIVE, so the block turns it
--             on inside the transaction and rolls that back with everything
--             else. This is the only way to test a rule that is off by design.)
-- BEGIN;
--   <fixture above>
--   UPDATE alert_rules SET active = true WHERE rule_code = 'promise_open_or_overdue';
--
--   INSERT INTO follow_ups (follow_up_id, promised_in, promise_text,
--                           promised_at, due_at, status, channel)
--   VALUES ('22222222-2222-2222-2222-222222222222',
--           '11111111-1111-1111-1111-111111111111', 'اتواصل معك',
--           now() - interval '2 days', now() + interval '10 hours',
--           'open', 'phone_call');
--
--   -- P6 . due_within_hours. Seeded 24, and the promise is due in 10.
--   SELECT count(*) AS must_be_one
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code = 'promise_open_or_overdue';
--   DELETE FROM alert_occurrences WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   UPDATE alert_rules SET params = params || '{"due_within_hours": 2}'::jsonb
--    WHERE rule_code = 'promise_open_or_overdue';
--   SELECT count(*) AS must_be_zero
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code = 'promise_open_or_overdue';
--   -- 0: a 2-hour horizon does not reach a promise due in 10.
--
--   -- P7 . include_overdue. Push the promise into the past.
--   UPDATE alert_rules SET params = params || '{"due_within_hours": 24}'::jsonb
--    WHERE rule_code = 'promise_open_or_overdue';
--   UPDATE follow_ups SET due_at = now() - interval '5 hours'
--    WHERE follow_up_id = '22222222-2222-2222-2222-222222222222';
--   DELETE FROM alert_occurrences WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   SELECT count(*) AS must_be_one
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code = 'promise_open_or_overdue';
--   -- 1: overdue is included by default, which is the whole reason this rule
--   --    replaced promise_due.
--   DELETE FROM alert_occurrences WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   UPDATE alert_rules SET params = params || '{"include_overdue": false}'::jsonb
--    WHERE rule_code = 'promise_open_or_overdue';
--   SELECT count(*) AS must_be_zero
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code = 'promise_open_or_overdue';
--   -- 0
--
--   -- P8 . include_undated. Clear due_at entirely.
--   UPDATE alert_rules SET params = params || '{"include_overdue": true}'::jsonb
--    WHERE rule_code = 'promise_open_or_overdue';
--   UPDATE follow_ups SET due_at = NULL
--    WHERE follow_up_id = '22222222-2222-2222-2222-222222222222';
--   DELETE FROM alert_occurrences WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   SELECT count(*) AS must_be_zero
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code = 'promise_open_or_overdue';
--   -- 0: undated promises are excluded by default.
--   UPDATE alert_rules SET params = params || '{"include_undated": true}'::jsonb
--    WHERE rule_code = 'promise_open_or_overdue';
--   SELECT count(*) AS must_be_one
--     FROM evaluate_alert_rules('11111111-1111-1111-1111-111111111111')
--    WHERE rule_code = 'promise_open_or_overdue';
--   -- 1
-- ROLLBACK;
-- NOTE: this block leaves promise_open_or_overdue ACTIVE until the ROLLBACK.
-- Do not run its statements piecemeal outside a transaction on a live database.

-- ---------------------------------------------------------------------------
-- B11 . AN INJECTED ALERT-FUNCTION FAILURE LOSES NOTHING -- AND YOU CAN SEE IT.
--       Full block in acceptance_pr1a.sql, A14: it needs a call_ingest_jobs
--       row, so it lives with the lease tests rather than being duplicated
--       here. NOT RUN -- it is a rollout gate, to be run on staging or on a
--       restored copy after runbook step 4 has applied 012 and 013 there.
--
--       A14 is ONE transaction driven by SAVEPOINTs, and that is what makes it
--       a test at all: ROLLBACK TO SAVEPOINT undoes only the failed call and
--       leaves the fixture in place, so the stamp can actually be SELECTed
--       afterwards. The earlier revision aborted and rolled back the whole
--       transaction -- fixture included -- and then "asserted" the stamp of a
--       row that no longer existed.
--
--       What it proves, in the order it matters:
--         0. POSITIVE CONTROL: with the REAL function the alert node's
--            statement returns one occurrence and stamps the job. Without this
--            step, step 1 "passes" whenever the FENCE fails to match -- a
--            silent zero, which is exactly the behaviour PR1B replaced;
--         1. the stamp cannot outrun the work. Break evaluate_alert_rules()
--            inside the transaction (DDL is transactional, so ROLLBACK TO
--            SAVEPOINT restores it), run the alert node's statement, and it
--            must RAISE -- not return zero rows quietly. Then, with the
--            transaction usable again and the fixture still there,
--            SELECT alerts_evaluated_at IS NULL and alerts_error. THAT
--            inspection is the assertion;
--         2. reconcile_alert_evaluations() finds the gap and closes it -- stamp
--            set, occurrences present -- and a second sweep does nothing,
--            because the job is stamped and the function deduplicates anyway.
--
--       The injected definition never leaves an open transaction. Committing it
--       stops rule evaluation for every call in the pipeline, silently, until
--       013 is re-applied.
-- ---------------------------------------------------------------------------
-- The standing check, on a live database: this is the follow-up queue's
-- backlog, and it should be empty after every 15-minute sweep.
SELECT count(*) FILTER (WHERE alerts_error IS NULL)     AS never_attempted,
       count(*) FILTER (WHERE alerts_error IS NOT NULL) AS failing,
       min(updated_at)                                  AS oldest
  FROM call_ingest_jobs
 WHERE status IN ('evaluated','judge_failed','dead_letter')
   AND interaction_id      IS NOT NULL
   AND alerts_evaluated_at IS NULL;

-- And what is failing, with the reason attached:
SELECT uniqueid, status, updated_at, left(alerts_error, 200) AS alerts_error
  FROM call_ingest_jobs
 WHERE alerts_error IS NOT NULL
 ORDER BY updated_at DESC
 LIMIT 50;

-- ---------------------------------------------------------------------------
-- B12 . A POISON ROW DOES NOT BLOCK THE BATCH, AND SAYS WHY.
--       Also in acceptance_pr1a.sql A14, step 2. reconcile_alert_evaluations()
--       must NOT raise: it must return a row carrying error_text, write the
--       same text to alerts_error, leave alerts_evaluated_at NULL so the job is
--       retried on the next sweep, and still process the healthy jobs in the
--       same batch.
--
--       THE POISON IS DATA, NOT A BROKEN FUNCTION. A globally broken function
--       takes the healthy job down with it, so it cannot demonstrate the one
--       thing B12 is named after. A14 instead gives ONE interaction a pass-1
--       payload whose "is_real_inquiry" and "quote_valid" are the string
--       "maybe" -- both are cast to boolean inside evaluate_alert_rules(), and
--       'maybe' is not a boolean literal, so the REAL function raises 22P02 on
--       that interaction and no other. It also gives that job an OLDER
--       updated_at than the healthy one, because the sweep works oldest-first
--       and head-of-line blocking is only tested if the poison is in FRONT.
--
--       This is the reason the function is PL/pgSQL and not one big statement.
--       One statement over a batch has head-of-line blocking: a single
--       interaction whose data makes a rule raise aborts the whole batch,
--       forever, and records nothing about why.
-- ---------------------------------------------------------------------------
-- Manual smoke test on a copy: reconcile a bounded batch and read what came
-- back. On a healthy database this returns zero rows because nothing is due.
-- The function is not addressable by uniqueid -- it takes a LIMIT and works the
-- OLDEST unstamped terminal jobs first -- so on a database with a real backlog
-- this reconciles REAL rows, holding FOR UPDATE SKIP LOCKED on them for the
-- rest of the transaction. That is safe (the live workflow skips them) but it
-- is not nothing: run it on a copy, or inside a transaction you will roll back.
-- SELECT * FROM reconcile_alert_evaluations(10);

-- ---------------------------------------------------------------------------
-- Backfill the rules over recent history (INSERTS suppressed/pending rows --
-- do this on a copy, or accept the rows).
-- ---------------------------------------------------------------------------
-- SELECT o.rule_code, count(*)
--   FROM interactions i
--  CROSS JOIN LATERAL evaluate_alert_rules(i.interaction_id) o
--  WHERE i.started_at > now() - interval '30 days'
--  GROUP BY o.rule_code
--  ORDER BY 2 DESC;

-- ---------------------------------------------------------------------------
-- Operational: the queue, and the daily read
-- ---------------------------------------------------------------------------
SELECT rule_code, delivery_status, count(*),
       min(created_at) AS first_seen, max(created_at) AS last_seen
  FROM alert_occurrences
 GROUP BY rule_code, delivery_status
 ORDER BY rule_code, delivery_status;

SELECT occurrence_id, rule_code, created_at, started_at, customer_phone_e164,
       agent_name, uniqueid, lead_temperature, products, real_ask_quote,
       left(summary_ar, 120) AS summary_ar
  FROM v_alert_queue
 ORDER BY created_at DESC
 LIMIT 50;

SELECT alert_day, rule_code, occurrences, pending, acknowledged, suppressed,
       jsonb_array_length(pending_items) AS items
  FROM v_alert_digest_daily
 WHERE alert_day >= (now() AT TIME ZONE 'Asia/Riyadh')::date - 7
 ORDER BY alert_day DESC, rule_code;

-- Working the queue is an UPDATE, not a webhook:
-- UPDATE alert_occurrences
--    SET delivery_status = 'acknowledged',
--        acknowledged_at = now(), acknowledged_by = '<name>', ack_note = '<what happened>'
--  WHERE occurrence_id = '<occurrence_id>';
