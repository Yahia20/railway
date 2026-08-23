-- Verification and acceptance queries for PR1A (leases).
-- Every SQL statement quoted in docs/PR1A-leases.md lives here so it can be
-- reviewed and run without copying out of prose. Read-only unless marked.
--
-- NOTHING in this file has been run against any database.
--
-- Sections marked MUTATES create their own fixture row and roll back. None of
-- them touches a live in-flight job: an acceptance test that expires every
-- lease in the table is not a test, it is an outage.
--
-- ONE EXCEPTION, stated here so it is not a surprise: A13 needs two real
-- sessions to see each other, so its fixture is COMMITTED and is removed by an
-- explicit cleanup at the end of that section. A13 is therefore a staging /
-- restored-copy test, not a production one.

-- ---------------------------------------------------------------------------
-- After applying db/migrations/012_call_job_leases.sql
-- ---------------------------------------------------------------------------

-- 1 . the new columns exist. Six from 012 (the lease and the split budgets)
--     and two from 013 (whether this job's alert rules have been evaluated --
--     they live on the job row because that is what they describe).
SELECT column_name, data_type, is_nullable, column_default
  FROM information_schema.columns
 WHERE table_name = 'call_ingest_jobs'
   AND column_name IN ('claim_token', 'claim_until', 'claimed_at',
                       'next_attempt_at', 'asr_attempts', 'judge_attempts',
                       'alerts_evaluated_at', 'alerts_error')
 ORDER BY column_name;
-- expect 8 rows -- 6 after 012, 8 after 013

-- 2 . the constraints: the status CHECK was replaced under a findable name, and
--     the two invariants were added
SELECT conname, pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conrelid = 'call_ingest_jobs'::regclass
   AND contype = 'c'
 ORDER BY conname;
-- expect exactly three:
--   call_ingest_jobs_attempts_nonneg
--   call_ingest_jobs_lease_shape
--   call_ingest_jobs_status_check   -- listing all of discovered / transcribing /
--     transcribed / evaluating / evaluated / asr_failed / judge_failed / dead_letter
--
-- lease_shape must name claimed_at in BOTH halves. Grep for it:
--   ... AND claimed_at IS NOT NULL ...   (the in-flight half)
--   ... AND claimed_at IS NULL     ...   (the not-in-flight half)
-- The pre-release version omitted the second one, which permitted 'evaluated'
-- + null token + a leftover claimed_at: the exact shape a release path that
-- forgot a line produces, i.e. the bug the constraint exists to catch. If the
-- string 'claimed_at IS NULL' does not appear, 012 did not replace an older
-- constraint of the same name -- drop it by hand and re-run 012.
--
-- If a FOURTH single-column status check appears here, migration 012 dropped
-- something it did not create. Snapshot this list BEFORE applying 012 (runbook
-- step 3) so the diff is readable.

-- 3 . the backfill attributed the legacy counter to the right stage
SELECT status,
       count(*)             AS rows,
       sum(retries)         AS legacy_retries,
       sum(asr_attempts)    AS asr_attempts,
       sum(judge_attempts)  AS judge_attempts
  FROM call_ingest_jobs
 GROUP BY status
 ORDER BY status;
-- asr_failed   : asr_attempts   must equal legacy_retries
-- judge_failed : judge_attempts must equal legacy_retries
-- transcribed / evaluated : asr_attempts >= 1

-- 4 . the indexes the claim and the recovery sweep ride on
SELECT indexname, indexdef
  FROM pg_indexes
 WHERE tablename = 'call_ingest_jobs'
 ORDER BY indexname;
-- expect idx_call_ingest_jobs_claimable
--          (status, next_attempt_at, discovered_at, uniqueid)   <- all four
--    and idx_call_ingest_jobs_lease
--          (claim_until) WHERE claim_until IS NOT NULL
--    and (after 013) idx_call_ingest_jobs_alerts_pending
--          (updated_at) WHERE alerts_evaluated_at IS NULL
--                         AND interaction_id IS NOT NULL

-- ---------------------------------------------------------------------------
-- POST-DEPLOY INVARIANTS.  Run these after every deploy and after any incident.
-- Each one must return ZERO rows. They are cheap; run them from a monitor.
-- ---------------------------------------------------------------------------

-- I1 . no malformed in-flight row (a lease is a three-column fact)
SELECT uniqueid, status, claim_token, claimed_at, claim_until
  FROM call_ingest_jobs
 WHERE status IN ('transcribing', 'evaluating')
   AND (claim_token IS NULL OR claimed_at IS NULL OR claim_until IS NULL);

-- I2 . no lease attached to a row that is not in flight
SELECT uniqueid, status, claim_token, claim_until
  FROM call_ingest_jobs
 WHERE status NOT IN ('transcribing', 'evaluating')
   AND (claim_token IS NOT NULL OR claim_until IS NOT NULL);

-- I3 . no exhausted row still sitting in a retryable status
SELECT uniqueid, status, asr_attempts, judge_attempts, left(last_error, 120)
  FROM call_ingest_jobs
 WHERE (status IN ('discovered', 'asr_failed')   AND asr_attempts   >= 5)
    OR (status IN ('transcribed', 'judge_failed') AND judge_attempts >= 5);

-- I4 . nothing has been holding a lease longer than the lease is meant to last.
--      The claim issues 10800 s; anything older than that plus the sweep
--      interval is a row the recovery sweep is failing to reach.
SELECT uniqueid, status, claimed_at, claim_until, now() - claimed_at AS held_for
  FROM call_ingest_jobs
 WHERE status IN ('transcribing', 'evaluating')
   AND claimed_at < now() - interval '4 hours'
 ORDER BY claimed_at;

-- I5 . the follow-up queue is not silently falling behind.
--      Alert evaluation is durable work (PR1B section 5): a terminal job whose
--      alerts_evaluated_at is still NULL is a call missing from the queue.
--      'Reconcile alert evaluations' runs every 15 minutes, so this drains to
--      zero on every sweep. A row here with alerts_error set is a real failure
--      with its reason attached; a row with alerts_error NULL that keeps
--      reappearing means the reconciliation node is not running at all.
SELECT uniqueid, status, updated_at, left(alerts_error, 200) AS alerts_error
  FROM call_ingest_jobs
 WHERE status IN ('evaluated', 'judge_failed', 'dead_letter')
   AND interaction_id      IS NOT NULL
   AND alerts_evaluated_at IS NULL
 ORDER BY updated_at
 LIMIT 50;

-- ---------------------------------------------------------------------------
-- A2 . a claimed row is invisible to the claim
-- ---------------------------------------------------------------------------
SELECT uniqueid, status, claim_token, claimed_at, claim_until, next_attempt_at
  FROM call_ingest_jobs
 WHERE status IN ('transcribing', 'evaluating')
   AND claim_until > now()
 ORDER BY claimed_at;
-- Re-run scripts/sql/02_claim_work.sql: none of these uniqueids may come back.

-- ---------------------------------------------------------------------------
-- A3 . A LOST LEASE CANNOT WRITE -- and not only to call_ingest_jobs.
--
-- This is the test the first version of PR1A did not have. Fencing the status
-- updates while leaving the transcript, pass-1, pass-2 and alert writes
-- unfenced meant a recovered stale execution could overwrite the new owner's
-- DATA and then discover it had lost the row only from an UPDATE 0 that nothing
-- read. Every one of the five writes below must report ZERO rows when the token
-- is wrong.
--
-- MUTATES nothing on success, but run it inside a transaction anyway: if a
-- fence is broken, the point of the test is that it writes.
-- ---------------------------------------------------------------------------
-- Pick a row that is genuinely in flight and note its real token:
SELECT uniqueid, interaction_id, status, claim_token
  FROM call_ingest_jobs
 WHERE status IN ('transcribing', 'evaluating')
 ORDER BY claimed_at DESC
 LIMIT 5;

-- BEGIN;
--   -- A3a . job status (this was already fenced)
--   -- run scripts/sql/02_mark_evaluated.sql with
--   --   $1 = <uniqueid>, $2 = gen_random_uuid()      -- deliberately wrong
--   -- expect UPDATE 0 / zero rows returned
--
--   -- A3b . transcript + interaction + job link
--   -- run scripts/sql/02_store_call_transcript.sql with
--   --   $1 = <the job's meta jsonb>, $2 = '{"full_text":"STALE WRITE",
--   --        "segments":[],"provider":"x","model_version":"x","confidence":0.9}',
--   --   $3 = <uniqueid>, $4 = gen_random_uuid(), $5 = 10800
--   -- expect zero rows. Then prove nothing moved:
--   SELECT full_text = 'STALE WRITE' AS must_be_false
--     FROM transcripts WHERE interaction_id = '<interaction_id>';
--
--   -- A3c . pass 1
--   -- run scripts/sql/02_store_pass1.sql with
--   --   $1 = <interaction_id>, $2 = '{"pass1":{"prompt_version":"STALE",
--   --        "model":"STALE","payload":{"intent":"other"}}}',
--   --   $3 = <uniqueid>, $4 = gen_random_uuid()
--   -- expect zero rows. Then:
--   SELECT prompt_version = 'STALE' AS must_be_false
--     FROM interaction_analysis WHERE interaction_id = '<interaction_id>';
--
--   -- A3d . pass 2
--   -- run scripts/sql/02_store_evaluation.sql with
--   --   $1 = <interaction_id>, $2 = '{"pass2":{"prompt_version":"STALE",
--   --        "model":"STALE","final_score":0,"payload":{}}}',
--   --   $3 = <uniqueid>, $4 = gen_random_uuid()
--   -- expect zero rows. Then:
--   SELECT prompt_version = 'STALE' AS must_be_false
--     FROM agent_evaluations WHERE interaction_id = '<interaction_id>';
--
--   -- A3e . the alert write. Its fence is "the job is still in the terminal
--   -- state this execution just wrote, and carries no lease".
--   -- run scripts/sql/02_evaluate_alert_rules.sql with
--   --   $1 = <interaction_id>, $2 = <uniqueid>, $3 = 'evaluated'
--   -- against a row that is currently 'evaluating' (i.e. re-claimed by
--   -- somebody else). Expect zero rows AND no new occurrence, AND no stamp:
--   SELECT count(*) AS must_be_unchanged
--     FROM alert_occurrences WHERE interaction_id = '<interaction_id>';
--   SELECT alerts_evaluated_at IS NULL AS must_be_true
--     FROM call_ingest_jobs WHERE uniqueid = '<uniqueid>';
--
--   -- A3f . THE RIGHT TOKEN IS NOT ENOUGH IF THE LEASE HAS LAPSED.
--   -- The fences require claim_until > now(), so an execution that overshot
--   -- its lease writes nothing even though nobody has swept the row yet --
--   -- otherwise the window between expiry and recovery is unfenced by
--   -- definition. Fixture, because this must not be done to a live row:
--   INSERT INTO call_ingest_jobs (uniqueid, filename, audio_uri, meta, status,
--                                 claim_token, claimed_at, claim_until)
--   VALUES ('ACC-A3F', 'a3f.wav', 'drive://a3f', '{"kind":"q"}'::jsonb,
--           'transcribing', gen_random_uuid(),
--           now() - interval '4 hours', now() - interval '1 minute')
--   RETURNING uniqueid, claim_token;   -- note the token; it is the CORRECT one
--
--   -- run scripts/sql/02_store_call_transcript.sql with $3 = 'ACC-A3F' and
--   -- $4 = <that token> -- the right token, an expired lease.
--   -- expect ZERO rows, and nothing written:
--   SELECT count(*) AS transcripts_must_be_zero FROM transcripts t
--     JOIN call_ingest_jobs j ON j.interaction_id = t.interaction_id
--    WHERE j.uniqueid = 'ACC-A3F';
-- ROLLBACK;

-- ---------------------------------------------------------------------------
-- A4 . recovery reopens a dead lease, and only dead-letters at the cap.
--      MUTATES -- creates and rolls back its OWN fixture rows. It must not
--      expire live leases: doing that to a production queue mid-batch causes
--      exactly the double-processing this PR exists to prevent.
-- ---------------------------------------------------------------------------
-- BEGIN;
--   INSERT INTO call_ingest_jobs
--     (uniqueid, filename, audio_uri, meta, status,
--      asr_attempts, judge_attempts, claim_token, claimed_at, claim_until)
--   VALUES
--     ('ACC-A4-retryable', 'a4a.wav', 'drive://a4a', '{"kind":"q"}'::jsonb,
--      'transcribing', 2, 0, gen_random_uuid(),
--      now() - interval '4 hours', now() - interval '1 minute'),
--     ('ACC-A4-at-cap',    'a4b.wav', 'drive://a4b', '{"kind":"q"}'::jsonb,
--      'transcribing', 5, 0, gen_random_uuid(),
--      now() - interval '4 hours', now() - interval '1 minute'),
--     ('ACC-A4-judge',     'a4c.wav', 'drive://a4c', '{"kind":"q"}'::jsonb,
--      'evaluating',   1, 2, gen_random_uuid(),
--      now() - interval '4 hours', now() - interval '1 minute');
--
--   -- then run scripts/sql/02_recover_expired_leases.sql with $1 = 45, $2 = 5
--
--   SELECT uniqueid, status, asr_attempts, judge_attempts,
--          next_attempt_at > now() + interval '44 minutes' AS cooled_down,
--          claim_token IS NULL AND claim_until IS NULL     AS lease_cleared,
--          left(last_error, 80)
--     FROM call_ingest_jobs
--    WHERE uniqueid LIKE 'ACC-A4-%'
--    ORDER BY uniqueid;
--   -- ACC-A4-retryable -> asr_failed    (attempts 2 < 5)
--   -- ACC-A4-at-cap    -> dead_letter   (attempts 5 >= 5)
--   -- ACC-A4-judge     -> judge_failed  (judge_attempts 2 < 5)
--   -- all three: lease_cleared = true, last_error LIKE 'lease_expired:%'
-- ROLLBACK;

-- ---------------------------------------------------------------------------
-- A5 . a judge retry must not re-transcribe.  MUTATES -- deliberate setup.
-- ---------------------------------------------------------------------------
-- Note the transcript timestamp first:
SELECT j.uniqueid, j.status, t.transcribed_at, t.asr_confidence
  FROM call_ingest_jobs j
  JOIN transcripts t ON t.interaction_id = j.interaction_id
 WHERE j.status = 'evaluated'
 ORDER BY j.updated_at DESC
 LIMIT 5;

-- Re-open one of them for the judge only:
-- UPDATE call_ingest_jobs
--    SET status = 'judge_failed', judge_attempts = 0,
--        claim_token = NULL, claimed_at = NULL, claim_until = NULL,
--        next_attempt_at = now()
--  WHERE uniqueid = '<uniqueid>'
-- RETURNING uniqueid, status;
-- Run the workflow once. transcribed_at must be UNCHANGED and the execution
-- log must show "Route by work stage" taking the `evaluate` output, with
-- "Cohere Arabic ASR" absent from the run entirely. "Begin judge attempt" must
-- report judge_attempts = 1, not 2: the claim spent the attempt, the handoff
-- must not spend a second one.

-- ---------------------------------------------------------------------------
-- A6 . a red transcript is stored for audit, then dead-lettered
-- ---------------------------------------------------------------------------
SELECT j.uniqueid,
       j.status,
       left(j.last_error, 120)                         AS reason,
       (t.interaction_id IS NOT NULL)                  AS transcript_stored,
       t.asr_confidence,
       t.asr_metrics->>'asr_quality_status'            AS quality,
       j.judge_attempts
  FROM call_ingest_jobs j
  LEFT JOIN transcripts t ON t.interaction_id = j.interaction_id
 WHERE j.last_error LIKE 'asr_quality_red:%'
 ORDER BY j.updated_at DESC;
-- every row: status = 'dead_letter' AND transcript_stored = true
-- AND judge_attempts UNCHANGED from before the run: the quality gates now run
-- BEFORE "Begin judge attempt", so an unusable transcript costs no judge budget.

-- ---------------------------------------------------------------------------
-- A7 . pass 1 survives a pass-2 failure
-- ---------------------------------------------------------------------------
SELECT j.uniqueid,
       j.status,
       left(j.last_error, 160)                    AS reason,
       (ia.interaction_id IS NOT NULL)            AS pass1_stored,
       (ae.interaction_id IS NOT NULL)            AS pass2_stored
  FROM call_ingest_jobs j
  LEFT JOIN interaction_analysis ia ON ia.interaction_id = j.interaction_id
  LEFT JOIN agent_evaluations   ae ON ae.interaction_id = j.interaction_id
 WHERE j.status IN ('judge_failed', 'dead_letter')
   AND j.interaction_id IS NOT NULL
 ORDER BY j.updated_at DESC;
-- a contract_failed row must show pass1_stored = true, pass2_stored = false

-- ---------------------------------------------------------------------------
-- A10 . GOLDEN CASES for the stored-transcript renderer.
--
-- "Load stored transcript" claims to be equivalent to
-- CallTranscript.as_dialogue() in services/worker/app/asr/cohere_arabic.py.
-- The first version was not, in four ways that each change what the judge
-- reads. This block runs the SAME expression the node runs against fixtures
-- and asserts the output byte for byte. It needs no tables and can be pasted
-- into any psql session.
-- ---------------------------------------------------------------------------
WITH fixture(name, segments, expected) AS (VALUES
  ('unknown speaker gets no label at all',
   '[{"seq": 0, "start_sec": 5, "text": "one"}]'::jsonb,
   '[00:05] one'),

  ('a known speaker is upper-cased and colon-suffixed',
   '[{"seq": 0, "start_sec": 5, "speaker": "agent", "text": "one"}]'::jsonb,
   '[00:05] AGENT: one'),

  ('a missing speaker key is "unknown", like the dataclass default',
   '[{"seq": 0, "start_sec": 65, "text": "one"}]'::jsonb,
   '[01:05] one'),

  -- as_dialogue skips `if not s.text` -- falsey, NOT whitespace-trimmed.
  ('whitespace-only text is KEPT',
   '[{"seq": 0, "start_sec": 0, "text": "   "}, {"seq": 1, "start_sec": 1, "text": "x"}]'::jsonb,
   E'[00:00]    \n[00:01] x'),

  ('empty and absent text are skipped',
   '[{"seq": 0, "start_sec": 0, "text": ""}, {"seq": 1, "start_sec": 1}, {"seq": 2, "start_sec": 2, "text": "x"}]'::jsonb,
   '[00:02] x'),

  -- Python iterates the LIST. Ordering by seq reordered a transcript whose seq
  -- was missing (NULL sorts last) or non-monotonic, and threw on a non-integer.
  ('array order wins over a missing or out-of-order seq',
   '[{"start_sec": 1, "text": "first"}, {"seq": 99, "start_sec": 2, "text": "second"}, {"seq": 3, "start_sec": 3, "text": "third"}]'::jsonb,
   E'[00:01] first\n[00:02] second\n[00:03] third'),

  -- lpad(x, 2, '0') TRUNCATES. 6187 s is 103:07; the old renderer said 10:07.
  ('a call past 99 minutes does not have its minutes truncated',
   '[{"seq": 0, "start_sec": 6187, "text": "late"}]'::jsonb,
   '[103:07] late'),

  ('fractional start_sec truncates toward zero, like int()',
   '[{"seq": 0, "start_sec": 59.999, "text": "x"}]'::jsonb,
   '[00:59] x'),

  ('an empty segment list renders as the empty string',
   '[]'::jsonb, '')
)
SELECT f.name,
       (r.rendered = f.expected) AS pass,
       f.expected,
       r.rendered
  FROM fixture f
  CROSS JOIN LATERAL (
    SELECT coalesce((
      SELECT string_agg(
               format('[%s:%s]%s %s',
                      lpad(d.mm::text, greatest(2, length(d.mm::text)), '0'),
                      lpad(d.ss::text, greatest(2, length(d.ss::text)), '0'),
                      CASE WHEN d.speaker = 'unknown'
                           THEN '' ELSE ' ' || upper(d.speaker) || ':' END,
                      d.txt),
               E'\n' ORDER BY d.ord)
      FROM (
        SELECT e.ord,
               coalesce(e.seg->>'text', '')                                  AS txt,
               coalesce(e.seg->>'speaker', 'unknown')                        AS speaker,
               trunc(coalesce((e.seg->>'start_sec')::numeric, 0))::bigint / 60 AS mm,
               trunc(coalesce((e.seg->>'start_sec')::numeric, 0))::bigint % 60 AS ss
        FROM jsonb_array_elements(f.segments) WITH ORDINALITY AS e(seg, ord)
      ) d
      WHERE d.txt <> ''
    ), '') AS rendered
  ) r
 ORDER BY f.name;
-- Every row must have pass = true. If one does not, the retry path and the
-- first-run path are rendering different transcripts, and every judge
-- comparison between them is measuring the renderer.

-- ---------------------------------------------------------------------------
-- A11 . the lease covers the batch.  MUTATES -- own fixture, rolled back.
--
-- The arithmetic the claim parameters encode (docs/PR1A-leases.md section 2):
-- 6 items x (900 s ASR timeout + 2.5 s batch interval) + 6 x 300 s judge
-- + DB = ~7275 s worst case, against a 10800 s lease. This test proves the
-- issued lease is long enough for the batch the claim actually takes, on a
-- MIXED batch -- some rows heading for ASR, some for the judge -- because that
-- is the case where the two stage timeouts add up.
-- ---------------------------------------------------------------------------
-- BEGIN;
--   INSERT INTO call_ingest_jobs (uniqueid, filename, audio_uri, meta, status,
--                                 next_attempt_at)
--   SELECT 'ACC-A11-' || i,
--          'a11-' || i || '.wav', 'drive://a11-' || i,
--          '{"kind":"q"}'::jsonb,
--          CASE WHEN i % 2 = 0 THEN 'discovered' ELSE 'transcribed' END,
--          now() - interval '1 minute'
--     FROM generate_series(1, 6) i;
--
--   -- then run scripts/sql/02_claim_work.sql with $1 = 6, $2 = 10800, $3 = 5
--
--   SELECT count(*)                                              AS claimed,
--          count(DISTINCT claim_token)                           AS distinct_tokens,
--          min(claim_until - now())                              AS shortest_lease,
--          count(*) FILTER (WHERE status = 'transcribing')       AS to_asr,
--          count(*) FILTER (WHERE status = 'evaluating')         AS to_judge,
--          sum(asr_attempts)                                     AS asr_attempts,
--          sum(judge_attempts)                                   AS judge_attempts
--     FROM call_ingest_jobs WHERE uniqueid LIKE 'ACC-A11-%';
--   -- claimed = 6, distinct_tokens = 6, shortest_lease >= 2:59:00,
--   -- to_asr = 3, to_judge = 3, asr_attempts = 3, judge_attempts = 3
--   -- (the claim spends the attempt for the stage it routes to, and only that
--   --  stage -- a mixed batch is where an off-by-one here shows up)
--
--   -- The boundary itself: a lease that has NOT yet expired must be invisible
--   -- to a second claim even when the work has been running longer than the old
--   -- 900 s lease would have allowed.
--   UPDATE call_ingest_jobs SET claimed_at = now() - interval '50 minutes'
--    WHERE uniqueid LIKE 'ACC-A11-%';
--   -- run 02_claim_work.sql again: zero ACC-A11-% rows may come back, and
--   -- 02_recover_expired_leases.sql must reclaim none of them either.
--   SELECT count(*) AS must_be_zero
--     FROM call_ingest_jobs
--    WHERE uniqueid LIKE 'ACC-A11-%' AND claim_until < now();
-- ROLLBACK;

-- ---------------------------------------------------------------------------
-- A12 . ZERO-ROW HANDOFF IS A HARD STOP.  MUTATES -- own fixture, rolled back.
--
-- The failure this prevents: a fenced write that matches nothing makes the n8n
-- Postgres node emit `{success:true}`, which is indistinguishable from success
-- to everything downstream. "Load stored transcript" would then run with an
-- undefined interaction_id -- either a null-transcript route or a uuid cast
-- error, depending on how n8n coerced the parameter that run.
-- ---------------------------------------------------------------------------
-- BEGIN;
--   INSERT INTO call_ingest_jobs (uniqueid, filename, audio_uri, meta, status,
--                                 claim_token, claimed_at, claim_until)
--   VALUES ('ACC-A12', 'a12.wav', 'drive://a12', '{"kind":"q"}'::jsonb,
--           'transcribing', gen_random_uuid(), now(), now() + interval '1 hour');
--
--   -- SQL side: every fenced write returns zero rows under a wrong token.
--   -- run 02_store_call_transcript.sql   with $3 = 'ACC-A12', $4 = gen_random_uuid()
--   -- run 02_begin_judge_attempt.sql     with $1 = 'ACC-A12', $2 = gen_random_uuid()
--   -- both: zero rows.
--
--   -- Budget side: the handoff also refuses to start a judge attempt that the
--   -- budget cannot pay for.
--   UPDATE call_ingest_jobs SET judge_attempts = 5 WHERE uniqueid = 'ACC-A12';
--   -- run 02_begin_judge_attempt.sql with the CORRECT token, $3 = 3600, $4 = 5
--   -- expect zero rows: right token, exhausted budget, no judge request.
--
--   -- A12c . AND THE ZERO ROW IS NOW ACTED ON, not just survived.
--   -- The false output of 'Judge attempt started?' has two causes and they are
--   -- not the same thing. Both halves must be checked, in this order, because
--   -- the second one is the dangerous one.
--
--   -- (i) exhausted budget + the CORRECT token -> explicit dead letter.
--   -- run scripts/sql/02_dead_letter_judge_budget.sql with
--   --   $1 = 'ACC-A12', $2 = <the row's real token>, $3 = 5
--   SELECT uniqueid, status, judge_attempts, last_error,
--          claim_token IS NULL AND claimed_at IS NULL
--                              AND claim_until IS NULL AS lease_cleared
--     FROM call_ingest_jobs WHERE uniqueid = 'ACC-A12';
--   -- status = 'dead_letter', lease_cleared = true,
--   -- last_error = 'judge_budget_exhausted_before_handoff'
--
--   -- (ii) a STALE token must change nothing, even with the budget exhausted.
--   -- Reset the fixture and run the same statement with a wrong token:
--   UPDATE call_ingest_jobs
--      SET status = 'transcribing', judge_attempts = 5,
--          claim_token = gen_random_uuid(), claimed_at = now(),
--          claim_until = now() + interval '1 hour', last_error = NULL
--    WHERE uniqueid = 'ACC-A12';
--   -- run 02_dead_letter_judge_budget.sql with $2 = gen_random_uuid()
--   -- expect UPDATE 0 / zero rows, and:
--   SELECT status = 'transcribing' AS must_be_true, last_error
--     FROM call_ingest_jobs WHERE uniqueid = 'ACC-A12';
--   -- Somebody else owns this row. Dead-lettering it here would be the exact
--   -- overwrite the whole PR removes.
--
--   -- (iii) and the statement must not fire on a row that still HAS budget --
--   -- both halves of its WHERE have to hold.
--   UPDATE call_ingest_jobs SET judge_attempts = 1 WHERE uniqueid = 'ACC-A12';
--   -- run 02_dead_letter_judge_budget.sql with the CORRECT token
--   -- expect zero rows: right token, budget remaining, nothing dead-lettered.
-- ROLLBACK;
--
-- Workflow side (run once against the live workflow, after activation):
-- take a job that is genuinely 'transcribing', let ASR return, and while the
-- execution is between "Cohere Arabic ASR" and "Store call + transcript" run
--   UPDATE call_ingest_jobs SET claim_token = gen_random_uuid()
--    WHERE uniqueid = '<uniqueid>';
-- The execution must finish with "Transcript stored?" taking its FALSE output
-- and no node after it in the run log. `transcripts` must be unchanged and no
-- alert occurrence may exist for that interaction.

-- ---------------------------------------------------------------------------
-- A13 . THE RECOVERY-VERSUS-UPSERT RACE.  TWO REAL SESSIONS.
--       MUTATES -- own fixture, COMMITTED, with a mandatory cleanup at the end.
--
-- This is the test whose absence let revision 2 ship an unlocked fence. Every
-- other test in this file runs in one session, and in one session a
-- read-then-write window is invisible: the write always finds what the read
-- found.
--
-- NOT RUN. Nothing in this file has been run against any database, this section
-- included. A13 is a ROLLOUT GATE: it has to be run on staging, or on a
-- restored copy, after runbook step 4 has applied 012 and 013 there.
--
-- WHERE TO RUN IT. Two psql terminals against a staging copy, or the two-
-- connection driver in A13z below. Not against production: A13a leaves a live
-- lease on its fixture row for the length of the test, and both halves hold a
-- row lock that the real recovery sweep would queue behind.
--
-- DO NOT TRY TO DO THIS IN n8n. The obvious trick -- one Postgres node holding
-- `BEGIN; <statement>` and a second node holding `COMMIT;` behind a Wait --
-- DOES NOT WORK, and is worse than not testing at all. n8n's Postgres nodes run
-- on a POOLED connection: the second node is not guaranteed the backend the
-- first one used. Best case the COMMIT lands on a different session and errors;
-- worst case it commits somebody else's transaction while the BEGIN is left
-- open, holding a row lock on call_ingest_jobs until the pool recycles the
-- connection. There is no supported way to pin an n8n Postgres node to a
-- session, so A13 needs psql. The earlier revision of this section recommended
-- exactly that n8n trick; it was wrong.
--
-- HOW THE TEST WORKS -- AND THE ONE THING THAT IS NOT CLOCK-INDEPENDENT.
-- The two statements disagree about the lease, on purpose:
--   * 02_store_call_transcript.sql needs it LIVE     -- claim_until > now()
--   * 02_recover_expired_leases.sql needs it EXPIRED -- claim_until < now()
-- Both read `now()`, which is TRANSACTION START time -- not statement time and
-- not clock time. So one committed row is LIVE to a transaction that began
-- before claim_until and EXPIRED to one that began after it. That is the whole
-- mechanism: one session begins inside the window, the other begins outside it,
-- and neither has to falsify the fixture to make its own statement qualify.
-- The previous revision instead expired the row by hand between the steps,
-- which is why it tested nothing: the writer's predicate rejected the row
-- immediately instead of blocking on it.
--
-- That is also the part that could NOT be made clock-independent. The recovery
-- predicate is a literal `claim_until < now()`, generated from the workflow
-- JSON; there is no parameter to move it and no session setting that shifts
-- now(). Making the row expirable for one session while it is still live for
-- the other therefore requires REAL WALL-CLOCK TIME to pass between the two
-- BEGINs. Parameterising the predicate would make the test clock-free and would
-- also stop it testing the statement that actually runs in production, which is
-- the only statement worth testing. So: real time, with guard rails.
--
-- The window below is 60 seconds, which is generous for typing; widen it to
-- interval '5 minutes' if you are working slowly, nothing else changes. Every
-- step that has to land inside or outside the window carries an explicit
-- window_ok or row-count assertion, so a MISSED WINDOW shows up as "this run is
-- invalid, start over" and never as a green result.
--
-- TWO PRECONDITIONS THAT ARE NOT ABOUT THE LEASE (round-4 review).
--
-- 1. THE ISOLATION LEVEL IS PART OF THE PROOF. Every claim this test makes --
--    now() fixed at BEGIN, the blocked waiter, the EvalPlanQual recheck against
--    the newer row version -- is READ COMMITTED behaviour. Under REPEATABLE
--    READ the blocked statement does not recheck and re-qualify; it raises a
--    serialization failure instead, and a run that ends in 40001 proves nothing
--    about the fence. `default_transaction_isolation` is a server setting
--    somebody can have changed, so BOTH sessions assert it explicitly:
--
--        SHOW transaction_isolation;     -- MUST print 'read committed'
--
--    Run it immediately after each BEGIN, inside the transaction. Outside one
--    it reports the default, not what this transaction is actually using.
--
-- 2. "EXACTLY ONE LOCK WAITER" IS NOT AN ASSERTION -- IT IS A COINCIDENCE
--    WAITING TO HAPPEN. The original pg_stat_activity check filtered only on
--    `datname = current_database() AND wait_event_type = 'Lock'`. On a staging
--    copy that anybody else is touching -- a stray psql, an n8n execution
--    against the same database, a leftover session from an earlier attempt --
--    an UNRELATED waiter satisfies it, and the test reports the fence locking
--    when it is not. Each half therefore records the backend PID of the session
--    that is SUPPOSED to block, with
--
--        SELECT pg_backend_pid();        -- run it in that session
--
--    and the other session filters pg_stat_activity by that pid. The assertion
--    then becomes "THIS pid is waiting on a Lock", which is the thing being
--    claimed, rather than "somebody is".
-- ---------------------------------------------------------------------------
-- FIXTURE. Run it in session 1 with autocommit on -- it must be COMMITTED
-- before either half starts, or the other session cannot see it at all.
--
-- INSERT INTO call_ingest_jobs (uniqueid, filename, audio_uri, meta, status,
--                               asr_attempts, claim_token, claimed_at,
--                               claim_until)
-- VALUES ('ACC-A13', 'a13.wav', 'drive://a13',
--         '{"uniqueid":"ACC-A13","kind":"q","audio_uri":"drive://a13",
--           "started_at":"2026-08-21T09:00:00+03:00",
--           "customer_phone_raw":"0500000013",
--           "customer_phone_e164":"+966500000013"}'::jsonb,
--         'transcribing', 1, gen_random_uuid(), now(),
--         now() + interval '60 seconds')
-- RETURNING uniqueid, claim_token, claim_until;
--   -- write down the token as T and the expiry as E. THE CLOCK STARTS HERE.
--
-- The writer's five parameters, used by both halves:
--   $1 = the meta jsonb above
--   $2 = '{"duration_seconds":42.5,"sample_rate_hz":16000,"channels":1,
--          "provider":"acceptance","model_version":"a13","confidence":0.91,
--          "full_text":"nass","segments":[],"diarization":"none",
--          "asr_metrics":{"asr_quality_status":"green"}}'::jsonb
--   $3 = 'ACC-A13'
--   $4 = T          (T2 in A13b)
--   $5 = 10800      -- the renewal, and the reason A13a can be written at all
--
-- A13a . THE WRITER GETS THE LOCK FIRST, RENEWS IT, AND RECOVERY BACKS OFF.
--
--   session 1:  BEGIN;
--               SHOW transaction_isolation;   -- MUST be 'read committed'.
--                                             -- Anything else invalidates the
--                                             -- run: see precondition 1 above.
--               SELECT now() < claim_until AS window_ok,
--                      claim_until - now() AS time_left
--                 FROM call_ingest_jobs WHERE uniqueid = 'ACC-A13';
--               -- window_ok MUST be true. False means you are already past E:
--               -- ROLLBACK, delete the fixture, recreate it with a longer
--               -- window, start over. A false here invalidates the run.
--               --
--               -- run scripts/sql/02_store_call_transcript.sql with $1..$5.
--               -- MUST return EXACTLY ONE row (uniqueid, interaction_id,
--               -- status = 'transcribing', judge_attempts). ZERO rows means the
--               -- fence rejected the lease -- also invalid, also start over.
--               -- The statement has now renewed claim_until to now() + 10800 s
--               -- and holds the job row FOR UPDATE.
--               -- *** DO NOT COMMIT. ***
--
--   session 2:  -- (a) wait out the ORIGINAL lease. Derived from the row itself
--               --     rather than from a guess about how long session 1 took.
--               --     RUN THIS OUTSIDE A TRANSACTION.
--               SELECT claim_until, clock_timestamp(),
--                      pg_sleep(greatest(0, extract(epoch from
--                                 (claim_until - clock_timestamp())) + 2))
--                 FROM call_ingest_jobs WHERE uniqueid = 'ACC-A13';
--               -- session 1 has not committed, so this still reads E, not the
--               -- renewed value. A plain SELECT does not block on FOR UPDATE.
--               --
--               -- (b) NOW open the transaction. The order matters: now() is
--               --     fixed at BEGIN, so a transaction that began before E
--               --     considers the lease live, the recovery predicate is
--               --     false in its own eyes, the row is never considered, and
--               --     the statement returns zero rows WITHOUT EVER BLOCKING --
--               --     a green-looking result that proves nothing.
--               SELECT pg_backend_pid();      -- write it down as PID2. Session
--                                             -- 1 filters on it below; without
--                                             -- that filter an unrelated waiter
--                                             -- passes the test for you.
--               BEGIN;
--               SHOW transaction_isolation;   -- MUST be 'read committed'.
--               SELECT now() > claim_until AS window_ok
--                 FROM call_ingest_jobs WHERE uniqueid = 'ACC-A13';
--               -- window_ok MUST be true.
--               --
--               -- run scripts/sql/02_recover_expired_leases.sql, $1 = 45, $2 = 5
--               -- *** IT MUST BLOCK. *** If it returns immediately, the fence
--               -- is not taking the row lock and the test has already failed.
--
--   session 1:  -- prove SESSION 2 SPECIFICALLY is blocked, and on what. Session
--               -- 1 is idle-in-transaction and can still run queries. Substitute
--               -- the PID2 you wrote down:
--               SELECT pid, state, wait_event_type, wait_event,
--                      left(query, 40) AS q
--                 FROM pg_stat_activity
--                WHERE pid = <PID2>;
--               -- EXACTLY ONE row (pid is the primary key of this view), and it
--               -- MUST show wait_event_type = 'Lock' with wait_event
--               -- 'transactionid' or 'tuple'. wait_event_type NULL, or a state
--               -- of 'idle in transaction', means session 2 answered instantly
--               -- and never blocked -- that IS the old unlocked fence. Record
--               -- it and stop.
--               --
--               -- Optional, and worth running once: the same query WITHOUT the
--               -- pid filter, to see whether anybody else on this database is
--               -- also waiting on a lock. If somebody is, the unfiltered
--               -- version of this check would have passed no matter what the
--               -- fence did.
--               COMMIT;
--
--   session 2:  -- unblocks. MUST report UPDATE 0 / zero rows. Postgres
--               -- re-evaluated `claim_until < now()` against the NEW row
--               -- version (EvalPlanQual); the writer renewed the lease three
--               -- hours into the future and this transaction's now() is still
--               -- the instant it began, so the row no longer qualifies and the
--               -- sweep correctly leaves it alone.
--               COMMIT;
--
--   either:     SELECT status,
--                      claim_token IS NOT NULL AS lease_still_held,
--                      claim_until > now()     AS lease_renewed,
--                      last_error
--                 FROM call_ingest_jobs WHERE uniqueid = 'ACC-A13';
--               -- 'transcribing', true, true, NULL.
--               -- Recovery did not reclaim it and wrote no 'lease_expired: ...'.
--               SELECT count(*) AS transcript_must_be_one
--                 FROM call_ingest_jobs j
--                 JOIN transcripts t ON t.interaction_id = j.interaction_id
--                WHERE j.uniqueid = 'ACC-A13';
--               -- 1: the write that held the lock survived intact.
--
-- A13b . RECOVERY GETS THE LOCK FIRST, AND THE WRITER'S FENCE RETURNS ZERO.
--
--   Reset the fixture (either session, autocommit). The window opens again
--   here, and this is where T2 comes from:
--
--   DELETE FROM transcripts WHERE interaction_id IN
--     (SELECT interaction_id FROM interactions
--       WHERE external_source = 'asterisk_drive' AND external_id = 'ACC-A13');
--   UPDATE call_ingest_jobs
--      SET status = 'transcribing', claim_token = gen_random_uuid(),
--          claimed_at = now(), claim_until = now() + interval '60 seconds',
--          interaction_id = NULL, last_error = NULL
--    WHERE uniqueid = 'ACC-A13'
--   RETURNING claim_token, claim_until;        -- the new token T2, expiry E2
--
--   session 1:  SELECT pg_backend_pid();      -- write it down as PID1. In this
--                                             -- half SESSION 1 is the one that
--                                             -- must block, so session 2
--                                             -- filters on this pid.
--               BEGIN;
--               SHOW transaction_isolation;   -- MUST be 'read committed'.
--               SELECT now() < claim_until AS window_ok
--                 FROM call_ingest_jobs WHERE uniqueid = 'ACC-A13';
--               -- MUST be true. This pins session 1's now() INSIDE the lease,
--               -- which is what will make the writer's fence QUALIFY at scan
--               -- time later and therefore BLOCK, instead of filtering the row
--               -- out and returning zero for the wrong reason.
--               -- Do NOT run the writer yet.
--
--   session 2:  -- wait past E2, outside a transaction, exactly as in A13a:
--               SELECT pg_sleep(greatest(0, extract(epoch from
--                                 (claim_until - clock_timestamp())) + 2))
--                 FROM call_ingest_jobs WHERE uniqueid = 'ACC-A13';
--               BEGIN;
--               SHOW transaction_isolation;   -- MUST be 'read committed'.
--               SELECT now() > claim_until AS window_ok
--                 FROM call_ingest_jobs WHERE uniqueid = 'ACC-A13';   -- true
--               -- run scripts/sql/02_recover_expired_leases.sql, $1 = 45, $2 = 5
--               -- returns ACC-A13 -> asr_failed, token cleared.
--               -- *** DO NOT COMMIT. ***
--
--   session 1:  -- run 02_store_call_transcript.sql with $4 = T2.
--               -- *** IT MUST BLOCK. *** Its own now() is inside the lease, so
--               -- `claim_until > now()` holds against the committed row
--               -- version and the statement reaches FOR UPDATE -- which is
--               -- session 2's lock. An immediate answer here means the fence
--               -- is not locking.
--
--   session 2:  -- confirm SESSION 1 is the one waiting now: the same
--               -- pg_stat_activity query as in A13a, run from session 2 this
--               -- time and filtered on the OTHER pid:
--               SELECT pid, state, wait_event_type, wait_event,
--                      left(query, 40) AS q
--                 FROM pg_stat_activity
--                WHERE pid = <PID1>;
--               -- one row, wait_event_type = 'Lock'. Then
--               COMMIT;
--
--   session 1:  -- unblocks. MUST return ZERO rows. The EvalPlanQual recheck
--               -- runs against the recovered row, whose claim_token is NULL,
--               -- so `claim_token = $4` fails, `lease` is empty, every
--               -- dependent CTE is empty, and nothing is written anywhere.
--               COMMIT;
--               -- 'Transcript stored?' turns that zero row into a hard stop.
--
--   either:     SELECT status,
--                      claim_token IS NULL     AS lease_cleared,
--                      interaction_id IS NULL  AS never_linked,
--                      left(last_error, 60)
--                 FROM call_ingest_jobs WHERE uniqueid = 'ACC-A13';
--               -- 'asr_failed', true, true, 'lease_expired: held as ...'
--               SELECT count(*) AS transcripts_must_be_zero
--                 FROM transcripts t
--                 JOIN interactions i ON i.interaction_id = t.interaction_id
--                WHERE i.external_source = 'asterisk_drive'
--                  AND i.external_id = 'ACC-A13';
--               -- 0. Under the OLD unlocked fence this is 1: the stale write
--               -- did not block, never rechecked, and overwrote the transcript
--               -- belonging to whoever re-claimed the row. That is the bug A13
--               -- exists to catch, and the only assertion in it that matters.
--
-- CLEAN UP. MANDATORY -- this fixture is committed, not rolled back. Jobs
-- before interactions: call_ingest_jobs.interaction_id references it.
-- DELETE FROM alert_occurrences WHERE interaction_id IN
--   (SELECT interaction_id FROM interactions
--     WHERE external_source = 'asterisk_drive' AND external_id = 'ACC-A13');
-- DELETE FROM transcripts WHERE interaction_id IN
--   (SELECT interaction_id FROM interactions
--     WHERE external_source = 'asterisk_drive' AND external_id = 'ACC-A13');
-- DELETE FROM call_ingest_jobs WHERE uniqueid = 'ACC-A13';
-- DELETE FROM interactions
--  WHERE external_source = 'asterisk_drive' AND external_id = 'ACC-A13';
-- -- and confirm, because a leftover ACC-A13 in 'transcribing' is a row the
-- -- recovery sweep will keep picking up:
-- SELECT count(*) AS must_be_zero FROM call_ingest_jobs WHERE uniqueid = 'ACC-A13';
-- SELECT count(*) AS must_be_zero FROM interactions
--  WHERE external_source = 'asterisk_drive' AND external_id = 'ACC-A13';
--
-- A13z . A SCAFFOLD, NOT A TEST. Read this label before you rely on it.
--
--        What follows is a CONNECTION HARNESS: it opens two persistent psql
--        sessions and gives you a way to feed statements to each. It contains a
--        literal `...` where the actual test goes, it runs no assertions, and
--        it therefore CANNOT PASS OR FAIL. Running it to completion tells you
--        nothing about the fence.
--
--        A13a and A13b above -- two terminals, read by a human against the
--        stated expectations -- remain THE test, and the rollout gate. This
--        exists only so that somebody without two terminals can drive the same
--        keystrokes; they still read the two logs themselves.
--
--        Why two PROCESSES: they are two guaranteed-persistent sessions, and
--        the fifos keep each one's stdin open, which is what keeps its
--        transaction open between statements. A pooled client (n8n) cannot do
--        this -- see the A13 header.
--
--        If you finish this into a real driver, the finishing work is the
--        assertions, not the plumbing: window_ok true at both BEGINs,
--        transaction_isolation 'read committed' in both sessions, the recorded
--        pid showing wait_event_type = 'Lock' while the other session is
--        mid-statement, the exact row counts (one row / zero rows) at each
--        step, and the final table state. Until those are in it, it stays a
--        scaffold and this label stays.
--
--        NOT RUN. Point PGURL at a staging copy.
--
--   #!/usr/bin/env bash
--   set -euo pipefail
--   : "${PGURL:?point this at a STAGING copy, never production}"
--   d=$(mktemp -d); mkfifo "$d/s1" "$d/s2"
--   psql "$PGURL" -X -e -f - < "$d/s1" > "$d/s1.log" 2>&1 &
--   psql "$PGURL" -X -e -f - < "$d/s2" > "$d/s2.log" 2>&1 &
--   exec 3> "$d/s1"; exec 4> "$d/s2"   # holding the write ends open is what
--                                      # keeps the two sessions alive
--   s1() { printf '%s\n' "$*" >&3; sleep 1; }
--   s2() { printf '%s\n' "$*" >&4; sleep 1; }
--   # ... then the statements from A13a / A13b in exactly the order above,
--   #     with `sleep 65` wherever the text says "wait out the lease".
--   #     psql reads the next command only when the current one has returned,
--   #     so a statement that BLOCKS holds up its own session and nothing else,
--   #     which is what makes the ordering reproducible.
--   exec 3>&-; exec 4>&-; wait
--   echo '--- session 1'; cat "$d/s1.log"
--   echo '--- session 2'; cat "$d/s2.log"
--
--   The first thing to check in the logs: the step that MUST BLOCK has to show
--   its result AFTER the other session's COMMIT. If both sessions' timestamps
--   say it answered first, it never blocked.

-- ---------------------------------------------------------------------------
-- A14 . AN INJECTED ALERT-FUNCTION FAILURE LOSES NOTHING -- AND YOU CAN SEE IT.
--       MUTATES -- own fixture, rolled back. ONE session, ONE transaction,
--       driven by SAVEPOINTs.
--
-- WHY SAVEPOINTS. The previous revision created the fixture and broke the
-- function in the same transaction it then aborted, and rolled everything back
-- without looking at anything: it claimed to assert "alerts_evaluated_at stayed
-- NULL" in a transaction where the job row no longer existed. ROLLBACK TO
-- SAVEPOINT undoes ONLY the failed call, leaves the transaction usable and the
-- fixture in place, and lets the stamp actually be SELECTed afterwards. That
-- inspection IS the test; without it there is nothing here.
--
-- What this proves, in order:
--   0. POSITIVE CONTROL -- with the real function, the node's statement stamps
--      the job and records one occurrence. Skip this and step 1 can "pass"
--      because the FENCE did not match, which is a silent zero: precisely the
--      failure mode PR1B replaced.
--   1. THE STAMP CANNOT OUTRUN THE WORK -- with the function broken the
--      statement RAISES, and alerts_evaluated_at is still NULL afterwards.
--   2. A POISON ROW records WHY in alerts_error, stays unstamped so it is
--      retried, and does NOT stop the healthy job in the same batch.
--   3. reconcile_alert_evaluations() closes the gap, and a second sweep is free.
--
-- DDL is transactional in Postgres, so the broken definition disappears at
-- ROLLBACK TO SAVEPOINT / ROLLBACK. NEVER run the CREATE OR REPLACE outside a
-- transaction: committing it stops rule evaluation for every call in the
-- pipeline, silently, until somebody re-applies 013.
--
-- BEFORE YOU START, on a shared database. reconcile_alert_evaluations() is not
-- addressable by uniqueid: it takes a LIMIT and works the OLDEST unstamped
-- terminal jobs first. Check the backlog:
--   SELECT count(*) AS backlog FROM call_ingest_jobs
--    WHERE status IN ('evaluated','judge_failed','dead_letter')
--      AND interaction_id IS NOT NULL AND alerts_evaluated_at IS NULL;
-- If that is not 0, the fixtures below are at the BACK of the queue: raise
-- p_limit above the backlog, and understand that the call reconciles REAL rows
-- too. It takes FOR UPDATE SKIP LOCKED on them for the rest of the transaction,
-- and everything it writes is undone by the final ROLLBACK -- safe, but not
-- free, and the live workflow can be made to skip those rows meanwhile. Prefer
-- a restored copy.
--
-- NOT RUN, like everything else in this file. B11/B12 in acceptance_pr1b.sql
-- are this same block, seen from the alerts side.
-- ---------------------------------------------------------------------------
-- BEGIN;
--   -- FIXTURE A, the healthy job. The pass-1 payload is the deterministic
--   -- fixture from acceptance_pr1b.sql ("DETERMINISTIC FIXTURE"), which fires
--   -- exactly one rule: hot_real_ask_promised.
--   <acceptance_pr1b.sql fixture: interaction / transcript /
--    interaction_analysis on 11111111-1111-1111-1111-111111111111>
--   INSERT INTO call_ingest_jobs (uniqueid, filename, audio_uri, meta, status,
--                                 interaction_id, asr_attempts, judge_attempts,
--                                 updated_at)
--   VALUES ('ACC-A14', 'a14.wav', 'drive://a14', '{"kind":"q"}'::jsonb,
--           'evaluated', '11111111-1111-1111-1111-111111111111', 1, 1,
--           now() - interval '1 minute');
--
--   -- The fence the node's statement uses MUST match, or every assertion below
--   -- is vacuous:
--   SELECT count(*) AS fence_must_be_one FROM call_ingest_jobs
--    WHERE uniqueid = 'ACC-A14'
--      AND interaction_id = '11111111-1111-1111-1111-111111111111'
--      AND claim_token IS NULL AND status = 'evaluated';
--
--   SAVEPOINT s_fixture;
--
--   -- 0 . POSITIVE CONTROL.
--   -- run scripts/sql/02_evaluate_alert_rules.sql with
--   --   $1 = '11111111-1111-1111-1111-111111111111', $2 = 'ACC-A14',
--   --   $3 = 'evaluated'
--   -- EXACTLY ONE row back, rule_code = 'hot_real_ask_promised',
--   -- alerts_evaluated_at NOT NULL. Zero rows means the fence did not match --
--   -- fix the fixture before going any further.
--   ROLLBACK TO SAVEPOINT s_fixture;
--
--   -- 1 . BREAK IT, AND WATCH THE STAMP NOT HAPPEN.
--   CREATE OR REPLACE FUNCTION evaluate_alert_rules(p_interaction_id uuid)
--   RETURNS SETOF alert_occurrences LANGUAGE sql VOLATILE AS $broken$
--     SELECT * FROM alert_occurrences WHERE (1 / 0) = 1;
--   $broken$;
--   SAVEPOINT s_direct;
--   -- run scripts/sql/02_evaluate_alert_rules.sql with the same three params.
--   -- IT MUST RAISE, SQLSTATE 22012 division_by_zero. A quiet zero rows here is
--   -- the OLD behaviour and the whole reason this test exists.
--   ROLLBACK TO SAVEPOINT s_direct;
--   -- the transaction is usable again, and BOTH the fixture and the broken
--   -- definition survive -- s_direct is inside the break, s_fixture is before it.
--
--   -- THE INSPECTION THE OLD A14 COULD NOT DO:
--   SELECT alerts_evaluated_at IS NULL AS stamp_must_still_be_null,
--          alerts_error
--     FROM call_ingest_jobs WHERE uniqueid = 'ACC-A14';
--   -- true, NULL -- the work did not happen, and nothing on the row claims it did.
--   SELECT count(*) AS occurrences_must_be_zero FROM alert_occurrences
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--   -- 0
--
--   ROLLBACK TO SAVEPOINT s_fixture;   -- real function back, fixture intact
--
--   -- 2 . THE POISON ROW, BESIDE THE HEALTHY JOB, IN ONE BATCH.
--   -- The poison is DATA, not a broken function: a global break takes the
--   -- healthy row down with it and therefore cannot show head-of-line blocking
--   -- being avoided. `(pv->>'quote_valid')::boolean` and
--   -- `(...->'real_ask'->>'is_real_inquiry')::boolean` are real casts inside
--   -- evaluate_alert_rules(), and 'maybe' is not a boolean literal, so the REAL
--   -- function raises 22P02 for THIS interaction and no other.
--   INSERT INTO interactions (interaction_id, external_source, external_id,
--                             channel, started_at, customer_phone_e164, handled_by)
--   VALUES ('22222222-2222-2222-2222-222222222222', 'acceptance',
--           'ACC-A14-POISON', 'phone_call', now() - interval '1 day',
--           '+966500000002', 'agent');
--   INSERT INTO transcripts (interaction_id, audio_uri, asr_provider,
--                            asr_model_version, full_text, segments, asr_metrics)
--   VALUES ('22222222-2222-2222-2222-222222222222', 'drive://acc-a14-poison',
--           'acceptance', 'test', 'nass', '[]'::jsonb,
--           '{"asr_quality_status": "green"}'::jsonb);
--   INSERT INTO interaction_analysis (interaction_id, schema_version,
--                                     prompt_version, model, input_type,
--                                     raw_response)
--   VALUES ('22222222-2222-2222-2222-222222222222', '1.0', 'acc', 'acc',
--           'call_transcript',
--           '{"intent": "price_inquiry",
--             "summary_ar": "...",
--             "real_ask": {"is_real_inquiry": "maybe",
--                          "products": ["umrah_package"],
--                          "evidence": [{"quote": "..."}]},
--             "commercial": {"lead_temperature": "hot"},
--             "promises_made_by_agent": [{"promise": "..."}],
--             "pass1_validation": {"real_ask_quote_valid": true,
--                                  "promises": [{"index": 0,
--                                                "quote_valid": "maybe"}]}
--            }'::jsonb);
--   INSERT INTO call_ingest_jobs (uniqueid, filename, audio_uri, meta, status,
--                                 interaction_id, asr_attempts, judge_attempts,
--                                 updated_at)
--   VALUES ('ACC-A14-POISON', 'a14p.wav', 'drive://a14p', '{"kind":"q"}'::jsonb,
--           'evaluated', '22222222-2222-2222-2222-222222222222', 1, 1,
--           now() - interval '2 minutes');
--   -- an OLDER updated_at than ACC-A14, so the sweep meets the poison FIRST.
--   -- That is the only ordering that tests head-of-line blocking. (If a trigger
--   -- overwrites updated_at on insert, set it afterwards with an explicit
--   -- UPDATE and re-read both rows before continuing.)
--
--   -- Prove the poison really is poison before relying on it:
--   SAVEPOINT s_poison_probe;
--   SELECT * FROM evaluate_alert_rules('22222222-2222-2222-2222-222222222222');
--   -- MUST RAISE 22P02 invalid_text_representation -- "invalid input syntax for
--   -- type boolean: \"maybe\"". If it returns rows instead, this plan
--   -- short-circuited the bad cast: fall back to the injected broken function
--   -- from step 1 for the error-recording half, and record that the batch half
--   -- could not be demonstrated that way.
--   ROLLBACK TO SAVEPOINT s_poison_probe;
--
--   SELECT * FROM reconcile_alert_evaluations(10) ORDER BY job_uniqueid;
--   -- TWO rows, and the FUNCTION ITSELF MUST NOT RAISE:
--   --   ACC-A14         occurrences_recorded >= 1, error_text NULL
--   --   ACC-A14-POISON  occurrences_recorded  = 0, error_text LIKE '22P02 %'
--   -- The per-row BEGIN/EXCEPTION subtransaction is what stops one bad
--   -- interaction from aborting the sweep. Without it the healthy row -- which
--   -- is BEHIND the poison row in updated_at order -- is never reached at all.
--
--   SELECT uniqueid,
--          alerts_evaluated_at IS NOT NULL AS stamped,
--          left(alerts_error, 80)          AS alerts_error
--     FROM call_ingest_jobs
--    WHERE uniqueid IN ('ACC-A14', 'ACC-A14-POISON')
--    ORDER BY uniqueid;
--   -- ACC-A14         stamped = true,  alerts_error NULL
--   -- ACC-A14-POISON  stamped = false, alerts_error '22P02 ...'
--   --   -> retried on the next sweep, with the reason attached to the row.
--   --   The stamp is what makes "retry" free and "lost" impossible.
--
--   SELECT count(*) AS occurrences_must_be_at_least_one
--     FROM alert_occurrences
--    WHERE interaction_id = '11111111-1111-1111-1111-111111111111';
--
--   -- 3 . A SECOND SWEEP IS FREE. ACC-A14 is stamped, so it is not selected --
--   --     and even if it were, evaluate_alert_rules() deduplicates on
--   --     ON CONFLICT (rule, version, interaction, fact hash).
--   SELECT count(*) AS second_sweep_must_be_zero
--     FROM reconcile_alert_evaluations(10)
--    WHERE job_uniqueid = 'ACC-A14';
--   -- 0. ACC-A14-POISON DOES come back -- it is unstamped on purpose. That is
--   --    the retry working, not a leak.
-- ROLLBACK;
--
-- COMMITTED-FIXTURE VARIANT. If you want to read the null stamp from a SECOND
-- connection -- proving it is durable state and not just uncommitted rows you
-- are looking at -- commit the two fixtures instead, run steps 1-3 with each
-- failing call wrapped in its own short BEGIN ... ROLLBACK, and clean up BY
-- HAND, because there is no outer ROLLBACK to do it for you:
--   DELETE FROM alert_occurrences   WHERE interaction_id IN
--     ('11111111-1111-1111-1111-111111111111',
--      '22222222-2222-2222-2222-222222222222');
--   DELETE FROM call_ingest_jobs    WHERE uniqueid IN ('ACC-A14','ACC-A14-POISON');
--   DELETE FROM interaction_analysis WHERE interaction_id IN (... the two ...);
--   DELETE FROM transcripts          WHERE interaction_id IN (... the two ...);
--   DELETE FROM interactions         WHERE interaction_id IN (... the two ...);
-- The one thing the committed variant must NOT include is the injected broken
-- function. That stays inside a transaction that ends in ROLLBACK, always.

-- ---------------------------------------------------------------------------
-- RUNBOOK STEP 8 / 9b . park the queue, and PUT IT BACK.
--
-- Step 8 parks the whole retryable queue two hours into the future so that the
-- two hand-picked rows are the only claimable work. Without the backup and the
-- restore below, that park is permanent: every cool-down that keeps a throttled
-- ASR provider from burning the backlog is simply gone, and the first activated
-- run looks deceptively quiet.
--
-- A REAL table, not TEMP: the n8n Postgres node runs on a pooled connection and
-- a temp table would not survive to step 9b.
--
-- AND IT IS NEVER DROPPED BLIND. The earlier revision opened step 8.0 with
-- `DROP TABLE IF EXISTS tmp_pr1a_next_attempt_backup`. Re-running step 8 after
-- an interrupted rollout would then destroy the ONLY copy of the real schedule
-- and immediately replace it with a snapshot of the already-parked queue --
-- silently, and unrecoverably. An existing backup table is a HARD STOP.
-- ---------------------------------------------------------------------------
-- Step 8.0a . does one already exist?
-- SELECT to_regclass('tmp_pr1a_next_attempt_backup') AS must_be_null;
--
--   NULL      -> continue to 8.0b.
--   NOT NULL  -> STOP. Do not drop it. Do not overwrite it. It is either a
--                leftover from an interrupted rollout -- in which case it is the
--                only record of the real schedule -- or somebody else's table
--                wearing the same name. Inspect it first:
--
--   SELECT count(*) AS rows, min(next_attempt_at), max(next_attempt_at)
--     FROM tmp_pr1a_next_attempt_backup;
--   SELECT j.uniqueid, j.status,
--          j.next_attempt_at AS live, b.next_attempt_at AS backed_up
--     FROM tmp_pr1a_next_attempt_backup b
--     JOIN call_ingest_jobs j ON j.uniqueid = b.uniqueid
--    WHERE j.next_attempt_at IS DISTINCT FROM b.next_attempt_at
--    LIMIT 20;
--   -- rows here mean the queue is STILL PARKED from the interrupted run. Run
--   -- step 9b with THIS table first, verify it, drop it, and only then start
--   -- step 8 again.
--   -- If you conclude it is stale, RENAME it -- never drop it:
--   ALTER TABLE tmp_pr1a_next_attempt_backup
--     RENAME TO tmp_pr1a_next_attempt_backup_20260822_1830;

-- Step 8.0b . save. Plain CREATE TABLE, no IF NOT EXISTS: a table that appeared
-- between 8.0a and here must fail this step loudly, not be silently reused.
-- CREATE TABLE tmp_pr1a_next_attempt_backup AS
-- SELECT uniqueid, next_attempt_at
--   FROM call_ingest_jobs
--  WHERE status IN ('discovered','transcribed','asr_failed','judge_failed');

-- Step 8.0c . it is not a backup until you have looked at it.
-- SELECT count(*) AS rows_backed_up,
--        count(*) FILTER (WHERE next_attempt_at IS NULL) AS null_schedule
--   FROM tmp_pr1a_next_attempt_backup;
-- SELECT count(*) AS must_equal_rows_backed_up
--   FROM call_ingest_jobs
--  WHERE status IN ('discovered','transcribed','asr_failed','judge_failed');
-- -- The two counts must be EQUAL. "rows_backed_up = 0" is only correct if the
-- -- retryable queue is genuinely empty; the second count is what tells you
-- -- which of the two it is. Do not run the park until they match.

-- Step 9b . restore, BEFORE activating.
-- IDEMPOTENT: it only touches rows that still differ from the backup, so a
-- second run is a no-op and a run after a partial restore finishes the job.
-- UPDATE call_ingest_jobs j
--    SET next_attempt_at = b.next_attempt_at
--   FROM tmp_pr1a_next_attempt_backup b
--  WHERE b.uniqueid = j.uniqueid
--    AND j.next_attempt_at IS DISTINCT FROM b.next_attempt_at
-- RETURNING j.uniqueid, j.status, j.next_attempt_at;
-- -- First run: the parked rows come back. RUN IT A SECOND TIME: it must return
-- -- ZERO rows. That is the idempotence check and it costs nothing.
--
-- -- (i) every backed-up row matches the live row again
-- SELECT count(*) AS must_be_zero
--   FROM call_ingest_jobs j
--   JOIN tmp_pr1a_next_attempt_backup b ON b.uniqueid = j.uniqueid
--  WHERE j.next_attempt_at IS DISTINCT FROM b.next_attempt_at;
--
-- -- (ii) nothing that was backed up has disappeared. A row deleted in between
-- --      is listed here so you SEE it, not so you panic: expect zero.
-- SELECT b.uniqueid
--   FROM tmp_pr1a_next_attempt_backup b
--   LEFT JOIN call_ingest_jobs j ON j.uniqueid = b.uniqueid
--  WHERE j.uniqueid IS NULL;
--
-- -- (iii) nothing retryable is still parked by step 8. The park was +2 hours
-- --       and the real cool-downs are 45 minutes, so anything beyond 90 minutes
-- --       is either a leftover park or a cool-down you can name.
-- SELECT uniqueid, status, next_attempt_at
--   FROM call_ingest_jobs
--  WHERE status IN ('discovered','transcribed','asr_failed','judge_failed')
--    AND next_attempt_at > now() + interval '90 minutes'
--  ORDER BY next_attempt_at;
-- -- The two rows step 8 re-opened are 'evaluated' by now and are not in scope.
-- -- Do not activate while this returns rows you cannot explain.
--
-- ONLY THEN, and only after (i)-(iii) have passed, drop it. Until they pass,
-- this table is the only copy of the schedule.
-- DROP TABLE tmp_pr1a_next_attempt_backup;

-- ---------------------------------------------------------------------------
-- POST-DEPLOY, DURING THE OVERLAPPING CRON WINDOW (runbook step 10).
--
-- The previous version of this check was
--   SELECT uniqueid, count(DISTINCT claim_token) FROM call_ingest_jobs
--    WHERE status IN ('transcribing','evaluating')
--    GROUP BY uniqueid HAVING count(DISTINCT claim_token) > 1;
-- uniqueid is the PRIMARY KEY, so every group is one row and every row has one
-- current token: it cannot return anything, ever, however broken the claim is.
-- Four checks that CAN fail replace it.
-- ---------------------------------------------------------------------------

-- T1 . one token per ROW, not one token per BATCH.
-- gen_random_uuid() is volatile and is evaluated per row inside the claim's
-- UPDATE. Hoist it into the CTE and every row of a batch shares a token, so one
-- execution's terminal write unfences all six. This is what notices.
SELECT claim_token,
       count(*)                              AS rows_sharing_it,
       array_agg(uniqueid ORDER BY uniqueid)  AS rows
  FROM call_ingest_jobs
 WHERE claim_token IS NOT NULL
 GROUP BY claim_token
HAVING count(*) > 1;
-- expect ZERO rows

-- T2 . one execution claims at most the batch size.
-- Every row claimed by one statement shares claimed_at to the microsecond
-- (now() is transaction time), so this groups by execution. A group larger than
-- 6 means either LIMIT stopped working or 'Claim work' lost executeOnce and ran
-- once per item of the recovery sweep's output.
SELECT claimed_at, count(*) AS claimed_together,
       array_agg(uniqueid ORDER BY uniqueid) AS rows
  FROM call_ingest_jobs
 WHERE claimed_at > now() - interval '2 hours'
 GROUP BY claimed_at
HAVING count(*) > 6;
-- expect ZERO rows

-- T3 . nothing is being transcribed by an execution that does not hold the row.
-- SAMPLED, not a proof: the row can legitimately move between the write and
-- your look, so treat one hit as "run it again" and a repeatable hit as a
-- finding. The direct proof of mutual exclusion is A13, which is run
-- deliberately rather than watched for.
SELECT j.uniqueid, j.status, j.claimed_at, t.transcribed_at, j.asr_attempts
  FROM transcripts t
  JOIN call_ingest_jobs j ON j.interaction_id = t.interaction_id
 WHERE t.transcribed_at > now() - interval '15 minutes'
   AND (j.status <> 'transcribing' OR t.transcribed_at < j.claimed_at)
 ORDER BY t.transcribed_at DESC;

-- T4 . the follow-up queue is not silently falling behind. Same query as I5,
-- aggregated: on a healthy pipeline it is zero after every sweep.
SELECT count(*) FILTER (WHERE alerts_error IS NULL)     AS never_attempted,
       count(*) FILTER (WHERE alerts_error IS NOT NULL) AS failing,
       min(updated_at)                                  AS oldest
  FROM call_ingest_jobs
 WHERE status IN ('evaluated','judge_failed','dead_letter')
   AND interaction_id      IS NOT NULL
   AND alerts_evaluated_at IS NULL;

-- ---------------------------------------------------------------------------
-- Operational: what the queue looks like right now
-- ---------------------------------------------------------------------------
SELECT status,
       count(*)                                                   AS n,
       count(*) FILTER (WHERE claim_until > now())                AS leased_now,
       count(*) FILTER (WHERE claim_until IS NOT NULL
                          AND claim_until <= now())               AS lease_expired,
       count(*) FILTER (WHERE next_attempt_at > now())            AS cooling_down,
       min(next_attempt_at)                                       AS next_due
  FROM call_ingest_jobs
 GROUP BY status
 ORDER BY status;

-- The dead-letter worklist, most recent first.
SELECT uniqueid, filename, asr_attempts, judge_attempts, retries,
       left(last_error, 200) AS reason, updated_at
  FROM call_ingest_jobs
 WHERE status = 'dead_letter'
 ORDER BY updated_at DESC
 LIMIT 50;

-- ---------------------------------------------------------------------------
-- ROLLBACK SUPPORT.  See docs/PR1A-leases.md section 7.
--
-- Re-enabling the OLD workflow while rows sit in 'transcribing' or 'evaluating'
-- is unsafe: the old claim does not know those statuses, so those rows become
-- invisible to it forever, and the old workflow re-transcribes every judge
-- retry. Drain them into statuses the old workflow understands FIRST.
-- ---------------------------------------------------------------------------
-- Step 1 . how much is in flight (do this before deciding anything)
SELECT status, count(*), min(claimed_at), max(claim_until)
  FROM call_ingest_jobs
 WHERE status IN ('transcribing', 'evaluating')
 GROUP BY status;

-- Step 2 . with the new workflow DEACTIVATED and every execution finished or
-- cancelled, force the sweep to reclaim everything:
-- UPDATE call_ingest_jobs
--    SET claim_until = now() - interval '1 second'
--  WHERE status IN ('transcribing', 'evaluating');
-- -- then run scripts/sql/02_recover_expired_leases.sql with $1 = 0, $2 = 5

-- Step 3 . prove the drain is complete. Must be zero before the old workflow
-- is switched back on.
SELECT count(*) AS must_be_zero
  FROM call_ingest_jobs
 WHERE status IN ('transcribing', 'evaluating');

-- Step 4 . 012 and 013 are NOT rolled back. Both are additive: the extra
-- columns, statuses, constraints and tables are invisible to the old workflow,
-- and dropping them would destroy the attempt history the next attempt at this
-- deploy needs. If the alert rules must stop evaluating:
-- UPDATE alert_rules SET active = false;
