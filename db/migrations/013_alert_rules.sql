-- 013 — score-free follow-up queue.
--
-- WHY SCORE-FREE. The agent score (pass 2) is a judgement produced by a model
-- against a rubric, and it is still moving: prompt v3 changed it, the validator
-- changes it again. A rule that fires on "final_score < 60" therefore fires on
-- prompt changes as much as on reality, and the first false positive teaches
-- the sales floor to ignore the queue. Every rule here fires on FACTS from
-- pass 1 — did the customer ask for a product, did the agent promise something,
-- what did they call about. No rule reads a score.
--
-- WHAT "QUOTE-VALIDATED" DOES AND DOES NOT COVER. The two hot-lead rules are
-- gated on the pass-1 validator's verdict that the quote they rest on is
-- verbatim from the conversation, because they assert that a customer said a
-- specific sentence. `complaint_or_cancellation` is NOT: pass 1 v5 has no
-- intent-evidence field, so it asserts only a classification, and it is seeded
-- as triage (`is_alert = false`) for exactly that reason. Do not read this file
-- as "every alerting fact is quote-validated" — that was the earlier claim and
-- it was wrong.
--
-- WHY THERE IS NO SENDER. Call recordings reach us roughly a day after the
-- call. Nothing here is real-time, so a webhook would be theatre: a "hot lead"
-- pager that fires 26 hours late trains people to ignore pagers. The unit is an
-- OCCURRENCE recorded in a queue that fills itself:
--
--   pending       in the follow-up queue, nobody has worked it yet
--   acknowledged  a human (or a nightly consumer) has taken it
--   suppressed    the rule is in dry-run: recorded as evidence, kept out of
--                 the queue
--
-- `v_alert_queue` is the working list. `v_alert_digest_daily` is the once-a-day
-- read — per Riyadh day, per rule, with the phone number, summary, products and
-- quote a human needs to act. Neither is populated by pushing anything
-- anywhere; workflow 02 only ever calls evaluate_alert_rules().

-- ---------------------------------------------------------------------------
-- alert_rules — the catalogue. Editable without touching the workflow.
--
--   active    evaluate this rule at all
--   is_alert  a match belongs in the follow-up queue. false records the
--             occurrence with delivery_status 'suppressed' — which is how you
--             dry-run a new rule against live traffic for a week and measure
--             its precision before anyone is asked to work its output.
--   params    thresholds and gates, read by evaluate_alert_rules(). Keep
--             behaviour here, not in the SQL, so tuning a rule is an UPDATE and
--             not a deploy. Every key seeded below is actually read by the
--             function; a parameter nobody reads is a lie in a config table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_rules (
  rule_code    text PRIMARY KEY,
  rule_version int  NOT NULL DEFAULT 1,
  description  text NOT NULL,
  is_alert     boolean NOT NULL DEFAULT true,
  active       boolean NOT NULL DEFAULT true,
  params       jsonb   NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- alert_occurrences — one row per (rule, version, interaction, fact state).
--
-- occurrence_hash is what makes re-evaluation free. Workflow 02 re-runs the
-- rules every time a call reaches a terminal state — which happens again on
-- every judge retry — and the hash of the facts that fired the rule collapses
-- those into the row that already exists. Change the FACTS (a second promise
-- appears, an open promise goes from due-soon to overdue) and the hash changes,
-- which is a genuinely new thing to look at.
--
-- rule_version is part of the key so that bumping a rule's version deliberately
-- re-queues history under the new definition instead of silently deduping
-- against occurrences that meant something else.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_occurrences (
  occurrence_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_code       text NOT NULL REFERENCES alert_rules(rule_code),
  rule_version    int  NOT NULL,
  interaction_id  uuid NOT NULL REFERENCES interactions(interaction_id) ON DELETE CASCADE,
  occurrence_hash text NOT NULL,
  fact_snapshot   jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  delivery_status text NOT NULL DEFAULT 'pending'
                  CHECK (delivery_status IN ('pending', 'acknowledged', 'suppressed')),
  acknowledged_at timestamptz,
  acknowledged_by text,
  ack_note        text,
  UNIQUE (rule_code, rule_version, interaction_id, occurrence_hash)
);

-- Idempotent repair path for a pre-release copy of this file, which created the
-- table with push semantics ('sent' / 'failed') and delivery_* columns. There
-- is no database anywhere that this applies to today; it exists so that if one
-- turns up, the semantics converge instead of two shapes coexisting.
ALTER TABLE alert_occurrences
  ADD COLUMN IF NOT EXISTS acknowledged_at timestamptz,
  ADD COLUMN IF NOT EXISTS acknowledged_by text,
  ADD COLUMN IF NOT EXISTS ack_note        text;

DO $$
DECLARE c record;
BEGIN
  -- Any single-column CHECK on delivery_status, whatever Postgres named it.
  FOR c IN
    SELECT con.conname
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    WHERE rel.relname = 'alert_occurrences'
      AND con.contype = 'c'
      AND con.conkey = ARRAY[(SELECT att.attnum
                                FROM pg_attribute att
                               WHERE att.attrelid = rel.oid
                                 AND att.attname  = 'delivery_status')]
  LOOP
    EXECUTE format('ALTER TABLE alert_occurrences DROP CONSTRAINT %I', c.conname);
  END LOOP;

  UPDATE alert_occurrences SET delivery_status = 'acknowledged'
   WHERE delivery_status = 'sent';
  UPDATE alert_occurrences SET delivery_status = 'pending'
   WHERE delivery_status = 'failed';

  ALTER TABLE alert_occurrences
    ADD CONSTRAINT alert_occurrences_delivery_status_check
    CHECK (delivery_status IN ('pending', 'acknowledged', 'suppressed'));
END $$;

COMMENT ON COLUMN alert_occurrences.delivery_status IS
  'Queue state, not push state. pending = waiting in the follow-up queue; acknowledged = a human took it; suppressed = the rule is in dry-run (is_alert = false). Nothing in this system sends anything: recordings arrive about a day after the call.';

CREATE INDEX IF NOT EXISTS idx_alert_occurrences_pending
    ON alert_occurrences (created_at)
    WHERE delivery_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_alert_occurrences_interaction
    ON alert_occurrences (interaction_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- The four rules.
--
-- ON CONFLICT DO NOTHING: re-running the migration must never overwrite a rule
-- somebody tuned in production. To change a rule, UPDATE it — and bump
-- rule_version if the change of MEANING should re-queue existing interactions.
-- ---------------------------------------------------------------------------
INSERT INTO alert_rules (rule_code, rule_version, description, is_alert, active, params) VALUES

  ('hot_real_ask_promised', 1,
   'Hot lead: the customer made a real, quotable tourism inquiry and the agent promised to come back to them. Somebody must actually come back.',
   true, true,
   '{"lead_temperatures": ["hot"], "require_real_ask_quote_valid": true, "require_valid_promise": true}'::jsonb),

  ('hot_real_ask_uncommitted', 1,
   'Hot lead left hanging: real, quotable inquiry, hot temperature, and the agent promised nothing. The expensive one — the call ended with no next step and nothing else in the pipeline will pick it up.',
   true, true,
   '{"lead_temperatures": ["hot"], "require_real_ask_quote_valid": true, "require_valid_promise": true, "require_asr_quality": "green"}'::jsonb),

  -- TRIAGE, not an alert. This rule rests on a classification with no quote
  -- behind it (see the header), so it is seeded is_alert = false: it records
  -- suppressed occurrences that can be sampled for precision, and it stays out
  -- of anyone's working queue until that sample says it earns a place there.
  -- The wording is hedged for the same reason.
  ('complaint_or_cancellation', 1,
   'Possible complaint/cancellation — review. Pass-1 classified the call as a complaint or a cancellation. There is no verbatim quote behind that classification, so this is a triage hint for a supervisor, not a finding.',
   false, true,
   '{"intents": ["complaint", "cancellation"]}'::jsonb),

  -- Replaces the old `promise_due`, which required due_at > now() and a
  -- two-hour future window: a sweep that missed that window could never alert
  -- on that promise again. This one includes promises that are ALREADY
  -- overdue, which is the state that actually matters when the source data is
  -- a day old. Seeded INACTIVE — see the description and docs/PR1B-alerts.md
  -- for exactly what has to exist before switching it on.
  ('promise_open_or_overdue', 1,
   'An open promise recorded against this conversation is due within the horizon or already overdue. INACTIVE: nothing materialises follow_ups today (workflow 03 is off, and it skips calls with a null agent_id), and no scheduled consumer calls evaluate_alert_rules() after they would be materialised. Activating it needs all three: workflow 03 running, its null-agent exclusion resolved, and a scheduled sweep.',
   false, false,
   '{"due_within_hours": 24, "include_overdue": true, "include_undated": false}'::jsonb)

ON CONFLICT (rule_code) DO NOTHING;

-- The rule this replaces. Retired rather than deleted, so that a pre-release
-- database keeps its occurrence history and simply stops evaluating it.
UPDATE alert_rules
   SET active = false, is_alert = false,
       description = 'RETIRED — superseded by promise_open_or_overdue. Its future-only two-hour window meant a sweep that missed the window could never fire on that promise at all.'
 WHERE rule_code = 'promise_due';

-- ---------------------------------------------------------------------------
-- LEGACY SEEDED-STATE REPAIR.
--
-- `ON CONFLICT DO NOTHING` above protects a rule somebody tuned in production.
-- It also means that on a database where an EARLIER copy of 013 already ran,
-- the seed silently does nothing and the rule keeps its old state. For
-- `complaint_or_cancellation` that old state is `is_alert = true`, which is the
-- one thing this revision of 013 deliberately changes: the rule rests on a
-- classification with no quote behind it, so it must record suppressed evidence
-- rather than put an unquoted accusation in a supervisor's queue.
--
-- This is a ONE-WAY correction of a pre-release seed, not a general reset. It
-- is narrowed three ways so it cannot clobber deliberate tuning:
--   * one named rule,
--   * still at rule_version 1 (a version bump means somebody redefined it),
--   * and only when it is not already in the intended state (idempotent: the
--     second run of this migration updates zero rows).
-- If you turned this rule on deliberately, turn it back on after applying --
-- and bump rule_version so the next migration leaves it alone.
UPDATE alert_rules
   SET is_alert    = false,
       description = 'Possible complaint/cancellation — review. Pass-1 classified the call as a complaint or a cancellation. There is no verbatim quote behind that classification, so this is a triage hint for a supervisor, not a finding.'
 WHERE rule_code    = 'complaint_or_cancellation'
   AND rule_version = 1
   AND is_alert IS DISTINCT FROM false;

-- Same class of gap, params side: an earlier seed of `hot_real_ask_uncommitted`
-- had no `require_asr_quality`. The function coalesces it to 'green', so the
-- behaviour is already right -- but the key must be PRESENT for the rule to be
-- tunable and for the params audit in acceptance_pr1b.sql to mean anything.
-- `'{...}'::jsonb || params` gives the STORED value precedence, so a tuned
-- value survives; only a missing key is filled in.
UPDATE alert_rules
   SET params = '{"require_asr_quality": "green"}'::jsonb || params
 WHERE rule_code = 'hot_real_ask_uncommitted'
   AND NOT (params ? 'require_asr_quality');

-- Verify the seeded state after applying (acceptance_pr1b.sql section 1):
--   SELECT rule_code, rule_version, is_alert, active, params
--     FROM alert_rules ORDER BY rule_code;

-- ---------------------------------------------------------------------------
-- evaluate_alert_rules — evaluate every active rule against one interaction,
-- record what fired, and return ONLY what was newly recorded.
--
-- It reads interaction_analysis.raw_response, which is the pass-1 payload
-- stored verbatim (workflow 02, node "Store pass1"); v_real_asks in 009 reads
-- the same shape. Nothing here reads agent_evaluations.
--
-- pass1_validation is written by the worker's pass-1 validator, which assigns it
-- INTO the payload dict itself (services/worker/app/evaluate/judge.py:
-- `payload["pass1_validation"] = validation`) before the payload is returned.
-- It is therefore inside interaction_analysis.raw_response, not beside it:
--   {"real_ask_quote_valid": bool,
--    "promises": [{"index": 0, "quote_valid": true}, ...],
--    "intent_evidence_valid": bool}
-- The worker ALSO lifts the same object out to `pass1.pass1_validation` for
-- convenience, and workflow 02 merges that sibling back in when it stores the
-- row, so the field is present whichever side a future worker writes it to.
--
-- Absent (older rows, or a worker without the validator) is treated as FALSE
-- wherever a rule is gated on it, so an un-validated extraction never reaches
-- somebody's working queue on a rule that claims a quote exists.
--
-- PARAMETERS ARE REAL. `require_real_ask_quote_valid` and `require_valid_promise`
-- are read, not decorative. Turning either off does not weaken the rule into
-- nonsense: it changes WHICH population the rule counts. With
-- require_valid_promise = false the rule counts every promise pass 1 extracted
-- instead of only the ones whose quote the validator confirmed — which is the
-- knob you want when measuring how much the validator is costing you.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION evaluate_alert_rules(p_interaction_id uuid)
RETURNS SETOF alert_occurrences
LANGUAGE sql VOLATILE AS $fn$
WITH rule AS (
  SELECT rule_code, rule_version, is_alert, active, params FROM alert_rules
),
base AS (
  SELECT i.interaction_id,
         i.customer_phone_e164,
         i.started_at,
         ia.raw_response AS p1,
         coalesce(t.asr_metrics->>'asr_quality_status', 'green') AS asr_quality_status
  FROM interactions i
  LEFT JOIN interaction_analysis ia ON ia.interaction_id = i.interaction_id
  LEFT JOIN transcripts          t  ON t.interaction_id  = i.interaction_id
  WHERE i.interaction_id = p_interaction_id
),
f AS (
  SELECT b.interaction_id,
         b.customer_phone_e164,
         b.started_at,
         b.asr_quality_status,
         b.p1->>'summary_ar' AS summary_ar,
         coalesce((b.p1->'real_ask'->>'is_real_inquiry')::boolean, false)               AS is_real_inquiry,
         lower(coalesce(b.p1->'commercial'->>'lead_temperature', 'unknown'))            AS lead_temperature,
         lower(coalesce(b.p1->>'intent', ''))                                           AS intent,
         coalesce((b.p1->'pass1_validation'->>'real_ask_quote_valid')::boolean, false)  AS real_ask_quote_valid,
         coalesce((b.p1->'pass1_validation'->>'intent_evidence_valid')::boolean, false) AS intent_evidence_valid,
         coalesce(b.p1->'real_ask'->'products', '[]'::jsonb)                            AS products,
         b.p1->'real_ask'->'evidence'->0->>'quote'                                      AS real_ask_quote,
         (SELECT count(*)
            FROM jsonb_array_elements(coalesce(b.p1->'pass1_validation'->'promises', '[]'::jsonb)) pv
           WHERE (pv->>'quote_valid')::boolean IS TRUE)                                 AS valid_promise_count,
         (SELECT coalesce(jsonb_agg(b.p1->'promises_made_by_agent'->((pv->>'index')::int)), '[]'::jsonb)
            FROM jsonb_array_elements(coalesce(b.p1->'pass1_validation'->'promises', '[]'::jsonb)) pv
           WHERE (pv->>'quote_valid')::boolean IS TRUE)                                 AS valid_promises,
         -- The unvalidated population, for when require_valid_promise is off.
         jsonb_array_length(coalesce(b.p1->'promises_made_by_agent', '[]'::jsonb))      AS all_promise_count,
         coalesce(b.p1->'promises_made_by_agent', '[]'::jsonb)                          AS all_promises
  FROM base b
),
cand AS (

  -- 1 · hot, real, quotable, and somebody promised something.
  SELECT r.rule_code,
         r.rule_version,
         CASE WHEN r.is_alert THEN 'pending' ELSE 'suppressed' END AS delivery_status,
         text_hash(f.interaction_id::text || '|hot_promised|' || f.lead_temperature
                   || '|' || g.promise_count::text)                AS occurrence_hash,
         jsonb_build_object(
           'lead_temperature',    f.lead_temperature,
           'products',            f.products,
           'real_ask_quote',      f.real_ask_quote,
           'promises',            g.promises,
           'promise_count',       g.promise_count,
           'quote_validated',     g.need_valid_ask,
           'summary_ar',          f.summary_ar,
           'customer_phone_e164', f.customer_phone_e164,
           'asr_quality_status',  f.asr_quality_status)            AS fact_snapshot
  FROM f
  CROSS JOIN rule r
  -- The two tunable gates, resolved once so the WHERE clause, the hash and the
  -- snapshot all agree about which population this rule is counting.
  CROSS JOIN LATERAL (
    SELECT coalesce((r.params->>'require_real_ask_quote_valid')::boolean, true) AS need_valid_ask,
           coalesce((r.params->>'require_valid_promise')::boolean, true)        AS need_valid_promise,
           CASE WHEN coalesce((r.params->>'require_valid_promise')::boolean, true)
                THEN f.valid_promise_count ELSE f.all_promise_count::bigint END AS promise_count,
           CASE WHEN coalesce((r.params->>'require_valid_promise')::boolean, true)
                THEN f.valid_promises ELSE f.all_promises END                   AS promises
  ) g
  WHERE r.rule_code = 'hot_real_ask_promised' AND r.active
    AND f.is_real_inquiry
    AND (NOT g.need_valid_ask OR f.real_ask_quote_valid)
    AND g.promise_count > 0
    AND f.customer_phone_e164 IS NOT NULL
    AND f.lead_temperature IN (SELECT jsonb_array_elements_text(r.params->'lead_temperatures'))

  UNION ALL

  -- 2 · hot, real, quotable — and NOTHING was promised. The dropped ball.
  -- Restricted to green ASR: on a red or amber transcript "the agent promised
  -- nothing" may simply mean the promise was in the audio we failed to decode,
  -- and accusing an agent of dropping a lead on the strength of a bad
  -- microphone is how you lose the sales floor's trust in the whole system.
  SELECT r.rule_code,
         r.rule_version,
         CASE WHEN r.is_alert THEN 'pending' ELSE 'suppressed' END,
         text_hash(f.interaction_id::text || '|hot_uncommitted|' || f.lead_temperature),
         jsonb_build_object(
           'lead_temperature',    f.lead_temperature,
           'products',            f.products,
           'real_ask_quote',      f.real_ask_quote,
           'summary_ar',          f.summary_ar,
           'customer_phone_e164', f.customer_phone_e164,
           'asr_quality_status',  f.asr_quality_status,
           'quote_validated',     g.need_valid_ask,
           'promise_count',       g.promise_count,
           'promises',            '[]'::jsonb)
  FROM f
  CROSS JOIN rule r
  -- The two tunable gates, resolved once so the WHERE clause, the hash and the
  -- snapshot all agree about which population this rule is counting.
  CROSS JOIN LATERAL (
    SELECT coalesce((r.params->>'require_real_ask_quote_valid')::boolean, true) AS need_valid_ask,
           coalesce((r.params->>'require_valid_promise')::boolean, true)        AS need_valid_promise,
           CASE WHEN coalesce((r.params->>'require_valid_promise')::boolean, true)
                THEN f.valid_promise_count ELSE f.all_promise_count::bigint END AS promise_count,
           CASE WHEN coalesce((r.params->>'require_valid_promise')::boolean, true)
                THEN f.valid_promises ELSE f.all_promises END                   AS promises
  ) g
  WHERE r.rule_code = 'hot_real_ask_uncommitted' AND r.active
    AND f.is_real_inquiry
    AND (NOT g.need_valid_ask OR f.real_ask_quote_valid)
    AND g.promise_count = 0
    AND f.customer_phone_e164 IS NOT NULL
    AND f.asr_quality_status = coalesce(r.params->>'require_asr_quality', 'green')
    AND f.lead_temperature IN (SELECT jsonb_array_elements_text(r.params->'lead_temperatures'))

  UNION ALL

  -- 3 · possible complaint or cancellation — TRIAGE. Intent comes from the
  -- pass-1 enum, which is a closed list — price_inquiry, booking_request,
  -- availability_check, complaint, support, modification, cancellation,
  -- general_info, other (prompts/pass1_customer_v5.md) — so this is set
  -- membership, never a substring match on free text.
  --
  -- Deliberately NOT gated on intent_evidence_valid: pass 1 v5 has no
  -- intent-evidence field, so the validator reports null for it and this rule
  -- would never fire. That is also precisely why the rule is seeded
  -- is_alert = false. The flag is recorded in the snapshot, so the day a quote
  -- is added the gate can be turned on with an UPDATE to params.
  SELECT r.rule_code,
         r.rule_version,
         CASE WHEN r.is_alert THEN 'pending' ELSE 'suppressed' END,
         text_hash(f.interaction_id::text || '|intent|' || f.intent),
         jsonb_build_object(
           'intent',                f.intent,
           'intent_evidence_valid', f.intent_evidence_valid,
           'quote_validated',       false,
           'summary_ar',            f.summary_ar,
           'lead_temperature',      f.lead_temperature,
           'customer_phone_e164',   f.customer_phone_e164,
           'asr_quality_status',    f.asr_quality_status)
  FROM f CROSS JOIN rule r
  WHERE r.rule_code = 'complaint_or_cancellation' AND r.active
    AND f.intent IN (SELECT jsonb_array_elements_text(r.params->'intents'))

  UNION ALL

  -- 4 · an open promise against this conversation is due soon, or is already
  -- overdue. The overdue half is the point: our source data is roughly a day
  -- old, so a rule that only ever looked FORWARD two hours could not fire on
  -- anything by the time we knew about it.
  --
  -- follow_ups rows are materialised by workflow 03 (nightly), so at the moment
  -- workflow 02 finalises a call there is normally nothing here yet. The rule
  -- is written against the interaction so the same
  -- evaluate_alert_rules(interaction_id) can be called by a scheduled sweep
  -- later; that sweep does not exist, which is why the rule is seeded inactive.
  SELECT r.rule_code,
         r.rule_version,
         CASE WHEN r.is_alert THEN 'pending' ELSE 'suppressed' END,
         -- The overdue flag is part of the hash: a promise that was "due soon"
         -- yesterday and is "overdue" today is a genuinely different thing to
         -- put in front of a human, and must not dedupe against itself.
         text_hash('follow_up|' || fu.follow_up_id::text || '|'
                   || CASE WHEN fu.due_at IS NULL THEN 'undated'
                           WHEN fu.due_at < now() THEN 'overdue'
                           ELSE 'due_soon' END),
         jsonb_build_object(
           'follow_up_id',        fu.follow_up_id,
           'promise_text',        fu.promise_text,
           'promised_at',         fu.promised_at,
           'due_at',              fu.due_at,
           'overdue',             (fu.due_at IS NOT NULL AND fu.due_at < now()),
           'hours_to_due',        CASE WHEN fu.due_at IS NULL THEN NULL
                                       ELSE round((extract(epoch from (fu.due_at - now())) / 3600.0)::numeric, 2)
                                  END,
           'summary_ar',          f.summary_ar,
           'customer_phone_e164', f.customer_phone_e164)
  FROM f
  CROSS JOIN rule r
  JOIN follow_ups fu ON fu.promised_in = f.interaction_id
  WHERE r.rule_code = 'promise_open_or_overdue' AND r.active
    AND fu.status = 'open'
    AND (
          (fu.due_at IS NOT NULL AND fu.due_at <  now()
             AND coalesce((r.params->>'include_overdue')::boolean, true))
       OR (fu.due_at IS NOT NULL AND fu.due_at >= now()
             AND fu.due_at <= now() + make_interval(
                   hours => coalesce((r.params->>'due_within_hours')::int, 24)))
       OR (fu.due_at IS NULL
             AND coalesce((r.params->>'include_undated')::boolean, false))
        )
)
INSERT INTO alert_occurrences (rule_code, rule_version, interaction_id,
                               occurrence_hash, fact_snapshot, delivery_status)
SELECT c.rule_code, c.rule_version, p_interaction_id,
       c.occurrence_hash, c.fact_snapshot, c.delivery_status
FROM cand c
ON CONFLICT (rule_code, rule_version, interaction_id, occurrence_hash) DO NOTHING
RETURNING *;
$fn$;

COMMENT ON FUNCTION evaluate_alert_rules(uuid) IS
  'Evaluate the active score-free rules against one interaction. Inserts what fired, deduplicated on (rule, version, interaction, fact hash), and returns ONLY the newly inserted rows — so a caller can act on each occurrence exactly once without asking "have I seen this already?".';

-- ---------------------------------------------------------------------------
-- ALERT EVALUATION IS DURABLE WORK, NOT BEST EFFORT.
--
-- The problem this section exists to solve. Workflow 02 evaluates the rules
-- AFTER the job has already reached a terminal state. A terminal job is never
-- claimed again and never recovered by the lease sweep, so if the rule
-- evaluation fails -- a transient connection drop, a deadlock, a bug in one
-- rule's SQL -- that call's occurrences are lost PERMANENTLY and nothing in the
-- system knows. The first version of the workflow made that worse by running
-- the alert node with `onError: continueRegularOutput` on a leaf: the error was
-- swallowed and the execution was reported green.
--
-- Two ways to fix it. We chose the second, and the reason is written down here
-- because it is the kind of decision that looks arbitrary six months later:
--
--   (a) Fold evaluate_alert_rules() into the same statement as the terminal
--       UPDATE. Genuinely atomic -- but it makes the FOLLOW-UP QUEUE a
--       precondition for FINISHING A JOB. One broken rule would then roll back
--       every terminalization, leave every job in 'evaluating' until its lease
--       lapsed, and have the sweep re-queue it for another DeepSeek judge run.
--       A cosmetic bug in a triage rule would become a pipeline outage that
--       spends money on every cycle. It also breaks the zero-row contract the
--       rest of this PR rests on: the terminal node would emit one row per
--       occurrence, so "no rule fired" and "the lease is gone" would look
--       identical to 'Job finalised?'.
--
--   (b) Record the evaluation as durable state on the job row, and reconcile
--       what is missing. Terminalization stays independent and cheap; a failed
--       evaluation is VISIBLE (`alerts_evaluated_at IS NULL` on a terminal job)
--       and RETRYABLE, and the retry is free because evaluate_alert_rules()
--       deduplicates on ON CONFLICT (rule, version, interaction, fact hash).
--
-- The invariant this buys: for every terminal job with an interaction,
-- alerts_evaluated_at is eventually NOT NULL, and the backlog is one query.
-- ---------------------------------------------------------------------------
ALTER TABLE call_ingest_jobs
  ADD COLUMN IF NOT EXISTS alerts_evaluated_at timestamptz,
  ADD COLUMN IF NOT EXISTS alerts_error        text;

COMMENT ON COLUMN call_ingest_jobs.alerts_evaluated_at IS
  'When the alert rules were LAST successfully evaluated for this job. Set only in the same statement as a successful evaluate_alert_rules() call, so it can never claim work that did not happen. NULL on a terminal job means the follow-up queue is missing that call - see reconcile_alert_evaluations().';
COMMENT ON COLUMN call_ingest_jobs.alerts_error IS
  'Why the last alert evaluation failed, recorded by reconcile_alert_evaluations(). Cleared on the next success. It is set from an exception handler, so it survives the rollback of the failed evaluation itself.';

-- The reconciliation backlog is looked at on every sweep, so it gets an index.
-- Partial: terminal jobs whose alerts have not been evaluated are meant to be a
-- vanishing minority, and the day they are not, this index is what keeps the
-- sweep from turning into a full scan of the whole job table.
CREATE INDEX IF NOT EXISTS idx_call_ingest_jobs_alerts_pending
    ON call_ingest_jobs (updated_at)
    WHERE alerts_evaluated_at IS NULL AND interaction_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- reconcile_alert_evaluations - re-run the rules for terminal jobs whose alert
-- evaluation never landed, one job at a time, and record what happened.
--
-- WHY PL/pgSQL AND NOT ONE BIG STATEMENT. A single statement over a batch has
-- head-of-line blocking: one interaction whose data makes a rule raise aborts
-- the whole batch, forever, and writes no evidence of why. The per-row
-- BEGIN/EXCEPTION block below is a subtransaction, so a poison row records its
-- own error in alerts_error, is skipped, and the rest of the batch still lands.
--
-- WHY IT IS SAFE TO RUN AT ANY TIME. evaluate_alert_rules() inserts under
-- ON CONFLICT (rule_code, rule_version, interaction_id, occurrence_hash)
-- DO NOTHING, so re-running it against a call that was already evaluated
-- records nothing new and returns zero rows. Re-running is free; not running is
-- what costs you.
--
-- LOCKING. It takes call_ingest_jobs rows with FOR UPDATE SKIP LOCKED, so it
-- never waits for a row that workflow 02 is holding -- it leaves it for the
-- next sweep. It never waits on a job row at all, which is what keeps it out of
-- the deadlock graph with the claim and the recovery sweep (see
-- docs/PR1A-leases.md, "Locking order").
--
-- The filter deliberately requires claim_token IS NULL: a job that has been
-- re-claimed is somebody else's work again, and its alerts will be evaluated by
-- whoever finalises it next.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION reconcile_alert_evaluations(p_limit int DEFAULT 200)
RETURNS TABLE (job_uniqueid          text,
               job_interaction_id    uuid,
               occurrences_recorded  int,
               error_text            text)
LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
  j record;
  n int;
BEGIN
  FOR j IN
    SELECT c.uniqueid AS u, c.interaction_id AS iid
      FROM call_ingest_jobs c
     WHERE c.status IN ('evaluated', 'judge_failed', 'dead_letter')
       AND c.interaction_id      IS NOT NULL
       AND c.alerts_evaluated_at IS NULL
       AND c.claim_token         IS NULL
     ORDER BY c.updated_at
     LIMIT p_limit
     FOR UPDATE SKIP LOCKED
  LOOP
    BEGIN
      SELECT count(*) INTO n FROM evaluate_alert_rules(j.iid) o;

      UPDATE call_ingest_jobs c
         SET alerts_evaluated_at = now(),
             alerts_error        = NULL,
             updated_at          = now()
       WHERE c.uniqueid = j.u;

      job_uniqueid         := j.u;
      job_interaction_id   := j.iid;
      occurrences_recorded := n;
      error_text           := NULL;
    EXCEPTION WHEN OTHERS THEN
      -- The subtransaction rolled back the failed evaluation. This UPDATE runs
      -- in the outer transaction, so the REASON survives even though the work
      -- did not -- which is the whole point: a silent zero is what we are
      -- replacing. alerts_evaluated_at stays NULL, so the row is retried on the
      -- next sweep.
      UPDATE call_ingest_jobs c
         SET alerts_error = left(SQLSTATE || ' ' || SQLERRM, 500),
             updated_at   = now()
       WHERE c.uniqueid = j.u;

      job_uniqueid         := j.u;
      job_interaction_id   := j.iid;
      occurrences_recorded := 0;
      error_text           := left(SQLSTATE || ' ' || SQLERRM, 500);
    END;
    RETURN NEXT;
  END LOOP;
END;
$fn$;

COMMENT ON FUNCTION reconcile_alert_evaluations(int) IS
  'Re-run the alert rules for terminal call_ingest_jobs whose alerts_evaluated_at is NULL, one job per subtransaction so a poison row records its error instead of aborting the batch. Idempotent: evaluate_alert_rules() deduplicates. Called by workflow 02, node "Reconcile alert evaluations".';

-- The backlog, in one query. This is the monitor: on a healthy pipeline it is
-- zero after every sweep.
--   SELECT count(*) FILTER (WHERE alerts_error IS NULL)     AS never_attempted,
--          count(*) FILTER (WHERE alerts_error IS NOT NULL) AS failing,
--          min(updated_at)                                  AS oldest
--     FROM call_ingest_jobs
--    WHERE status IN ('evaluated','judge_failed','dead_letter')
--      AND interaction_id IS NOT NULL
--      AND alerts_evaluated_at IS NULL;

-- ---------------------------------------------------------------------------
-- v_alert_queue — the follow-up queue: everything a human still has to work,
-- with the call facts an action needs so nobody has to re-derive them.
--
-- 'suppressed' is excluded (that is a dry-running rule) and 'acknowledged' is
-- excluded (somebody took it). There is no 'failed': nothing is sent, so
-- nothing can fail to send.
-- ---------------------------------------------------------------------------
-- DROP, not CREATE OR REPLACE. `CREATE OR REPLACE VIEW` can only add trailing
-- columns: it refuses to run when a column was removed, renamed, retyped or
-- reordered, with "cannot change name of view column". The pre-release shape of
-- this view carried the push-era delivery columns and no `promise_overdue`, so
-- on any database where that version exists a replace fails and the migration
-- stops half-applied. Dropping first is safe here because nothing else depends
-- on either view -- if that ever stops being true the DROP fails loudly rather
-- than cascading, which is the outcome we want.
DROP VIEW IF EXISTS v_alert_queue;
CREATE VIEW v_alert_queue AS
SELECT o.occurrence_id,
       o.rule_code,
       o.rule_version,
       r.description                              AS rule_description,
       o.created_at,
       o.delivery_status,
       i.interaction_id,
       i.channel,
       i.started_at,
       i.customer_phone_e164,
       coalesce(a.full_name, 'unassigned')        AS agent_name,
       j.uniqueid,
       o.fact_snapshot->>'summary_ar'             AS summary_ar,
       coalesce(o.fact_snapshot->'products', '[]'::jsonb) AS products,
       o.fact_snapshot->>'real_ask_quote'         AS real_ask_quote,
       o.fact_snapshot->>'lead_temperature'       AS lead_temperature,
       (o.fact_snapshot->>'overdue')::boolean     AS promise_overdue,
       o.fact_snapshot
FROM alert_occurrences o
JOIN interactions i          ON i.interaction_id = o.interaction_id
LEFT JOIN alert_rules r      ON r.rule_code = o.rule_code
LEFT JOIN agents a           ON a.agent_id = i.agent_id
LEFT JOIN call_ingest_jobs j ON j.interaction_id = i.interaction_id
WHERE o.delivery_status = 'pending'
ORDER BY o.created_at DESC;

-- ---------------------------------------------------------------------------
-- v_alert_digest_daily — the once-a-day read, grouped per Riyadh day and rule.
--
-- Intended for Metabase and for a nightly workflow that renders one message per
-- day. It is a VIEW, not a job: whoever reads it decides when, and re-reading
-- it changes nothing. `alert_day` is the day the occurrence was QUEUED, not the
-- day of the call — recordings land about a day late, and the person working
-- the list cares about what arrived on their desk today. The call's own
-- timestamp travels inside each item.
--
-- Asia/Riyadh is +03 with no DST, which is the same offset
-- PORTAL_TZ_OFFSET_HOURS defaults to in the worker.
-- ---------------------------------------------------------------------------
-- DROP first, same reason as v_alert_queue above.
DROP VIEW IF EXISTS v_alert_digest_daily;
CREATE VIEW v_alert_digest_daily AS
SELECT (o.created_at AT TIME ZONE 'Asia/Riyadh')::date                    AS alert_day,
       o.rule_code,
       r.description                                                     AS rule_description,
       count(*)                                                          AS occurrences,
       count(*) FILTER (WHERE o.delivery_status = 'pending')             AS pending,
       count(*) FILTER (WHERE o.delivery_status = 'acknowledged')        AS acknowledged,
       count(*) FILTER (WHERE o.delivery_status = 'suppressed')          AS suppressed,
       coalesce(
         jsonb_agg(
           jsonb_build_object(
             'occurrence_id',       o.occurrence_id,
             'interaction_id',      i.interaction_id,
             'uniqueid',            j.uniqueid,
             'started_at',          i.started_at,
             'customer_phone_e164', i.customer_phone_e164,
             'agent_name',          coalesce(a.full_name, 'unassigned'),
             'summary_ar',          o.fact_snapshot->>'summary_ar',
             'products',            coalesce(o.fact_snapshot->'products', '[]'::jsonb),
             'real_ask_quote',      o.fact_snapshot->>'real_ask_quote',
             'lead_temperature',    o.fact_snapshot->>'lead_temperature',
             'promise_text',        o.fact_snapshot->>'promise_text',
             'due_at',              o.fact_snapshot->>'due_at',
             'overdue',             o.fact_snapshot->'overdue')
           ORDER BY i.started_at)
         FILTER (WHERE o.delivery_status = 'pending'),
         '[]'::jsonb)                                                    AS pending_items
FROM alert_occurrences o
JOIN interactions i          ON i.interaction_id = o.interaction_id
LEFT JOIN alert_rules r      ON r.rule_code = o.rule_code
LEFT JOIN agents a           ON a.agent_id = i.agent_id
LEFT JOIN call_ingest_jobs j ON j.interaction_id = i.interaction_id
GROUP BY 1, 2, 3
ORDER BY 1 DESC, 2;

COMMENT ON VIEW v_alert_digest_daily IS
  'One row per Riyadh day per rule: counts by queue state plus the pending occurrences themselves, with phone, summary, products and quote. Read by Metabase or a nightly digest workflow. There is no sender in the pipeline — this view IS the delivery mechanism.';
