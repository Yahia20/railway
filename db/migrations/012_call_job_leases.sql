-- 012 — leased, resumable call jobs.
--
-- 008 made every recording a unit of recoverable work. It did not make that
-- work SAFE TO RUN TWICE. The claim step only stamped updated_at, so two
-- overlapping executions of workflow 02 (the schedule fires every 15 minutes;
-- a batch of 25 recordings can take longer than that) selected the same rows
-- and both transcribed and both judged them: double Cohere spend, double
-- DeepSeek spend, and two writers racing for one transcripts row.
--
-- This migration adds the three things a queue needs and 008 lacked:
--
--   1. In-flight statuses.  'transcribing' and 'evaluating' say a worker holds
--      this row RIGHT NOW. Without them a crashed execution is
--      indistinguishable from a healthy one.
--   2. A lease.  claim_token / claimed_at / claim_until. Every terminal write
--      is guarded by "AND claim_token = $token", so an execution that lost its
--      lease (it hung past claim_until and the row was recovered) can no longer
--      overwrite the state of whoever picked the work up afterwards.
--   3. Per-stage attempt counters.  One shared `retries` counter could not tell
--      "ASR failed five times" from "ASR succeeded once and the judge failed
--      four times", so a judge bug burned the ASR retry budget of the whole
--      backlog. asr_attempts and judge_attempts are counted where they are
--      spent, and are incremented AT CLAIM TIME — a worker that dies without
--      writing anything still consumed an attempt, which is what stops a
--      poison row from being claimed forever. The transcribe->evaluate handoff
--      inside one execution (workflow 02, "Begin judge attempt") counts the
--      judge attempt itself and keeps the same lease, so a crash after ASR but
--      before the judge is recovered as judge_failed, not re-transcribed. That
--      handoff runs AFTER the transcript quality gates: a red or low-confidence
--      transcript must not spend a judge attempt on a judge that is never
--      called.
--
-- `retries` is kept and still incremented on failure: existing dashboards and
-- the dead-letter query in the runbook read it. It is no longer authoritative.
--
-- Idempotent: safe to run twice, safe to run against a table already migrated.

-- ---------------------------------------------------------------------------
-- 1 · statuses
-- ---------------------------------------------------------------------------
-- 008 declared the CHECK inline, so its name is whatever Postgres generated.
-- Drop whatever check constraint currently governs the `status` column, then
-- add the new one under a stable name we can find next time.
DO $$
DECLARE c record;
BEGIN
  FOR c IN
    SELECT con.conname
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid = rel.relnamespace
    WHERE rel.relname = 'call_ingest_jobs'
      AND ns.nspname = current_schema()
      AND con.contype = 'c'
      -- Only constraints whose sole column IS `status`. Matching on the
      -- constraint TEXT would also catch an unrelated multi-column check that
      -- happens to mention the word, and dropping that would be silent damage.
      AND con.conkey = ARRAY[(SELECT att.attnum
                                FROM pg_attribute att
                               WHERE att.attrelid = rel.oid
                                 AND att.attname  = 'status')]
  LOOP
    EXECUTE format('ALTER TABLE call_ingest_jobs DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$;

ALTER TABLE call_ingest_jobs
  ADD CONSTRAINT call_ingest_jobs_status_check
  CHECK (status IN ('discovered',      -- listed on Drive, nothing done yet
                    'transcribing',    -- leased, ASR in flight
                    'transcribed',     -- interaction + transcript stored
                    'evaluating',      -- leased, judge in flight
                    'evaluated',       -- terminal success
                    'asr_failed',      -- retryable: transcript empty / low confidence
                    'judge_failed',    -- retryable: judge 422 or contract failure
                    'dead_letter'));   -- attempts burned, or unscoreable audio

-- ---------------------------------------------------------------------------
-- 2 · lease + scheduling columns
-- ---------------------------------------------------------------------------
ALTER TABLE call_ingest_jobs
  ADD COLUMN IF NOT EXISTS claimed_at      timestamptz,
  ADD COLUMN IF NOT EXISTS claim_until     timestamptz,
  ADD COLUMN IF NOT EXISTS claim_token     uuid,
  ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS asr_attempts    int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS judge_attempts  int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_error      text;

-- Single-quoted literals on one logical line each. Postgres does concatenate two
-- string constants separated by a newline, but not every SQL splitter that a
-- migration might travel through does, and a comment is not worth the risk.
COMMENT ON COLUMN call_ingest_jobs.claim_token IS
  'Lease identity. Every terminal UPDATE must carry "AND claim_token = $token": an execution that lost its lease must not be able to write.';
COMMENT ON COLUMN call_ingest_jobs.next_attempt_at IS
  'Earliest time this row may be claimed again. Set to now()+cooldown on every failure so a throttled ASR provider cannot burn the whole backlog in an hour.';
COMMENT ON COLUMN call_ingest_jobs.asr_attempts IS
  'Incremented when the row is claimed for transcription, not when ASR returns: a worker that dies silently still spent an attempt.';
COMMENT ON COLUMN call_ingest_jobs.judge_attempts IS
  'Incremented when evaluation actually begins - at claim time for a re-claimed transcript, or at the transcribe->evaluate handoff ("Begin judge attempt"), which runs after the transcript quality gates so an unusable transcript never spends a judge attempt.';
COMMENT ON COLUMN call_ingest_jobs.retries IS
  'Legacy combined counter, kept so existing dashboards and the dead-letter runbook query keep working. Still incremented on every failure, but asr_attempts and judge_attempts are the authoritative budgets.';

-- ---------------------------------------------------------------------------
-- 3 · backfill the split counters from the single legacy counter
-- ---------------------------------------------------------------------------
-- A row sitting in asr_failed burned its retries on ASR; a row in judge_failed
-- burned them on the judge. Anything else (discovered / transcribed / evaluated
-- / dead_letter) cannot be attributed, and is left at zero rather than guessed:
-- over-counting would dead-letter healthy rows on their first real failure.
-- Guarded on "= 0" so a re-run never double-counts.
UPDATE call_ingest_jobs
   SET asr_attempts = retries
 WHERE status = 'asr_failed' AND asr_attempts = 0 AND retries > 0;

UPDATE call_ingest_jobs
   SET judge_attempts = retries
 WHERE status = 'judge_failed' AND judge_attempts = 0 AND retries > 0;

-- Rows already past ASR have, by definition, transcribed successfully once.
UPDATE call_ingest_jobs
   SET asr_attempts = 1
 WHERE status IN ('transcribed','evaluated') AND asr_attempts = 0;

-- ---------------------------------------------------------------------------
-- 4 · invariants the queue cannot be allowed to violate quietly
-- ---------------------------------------------------------------------------
-- Both of these describe things that are already true of every code path in
-- workflow 02. They exist so that the day one of those paths changes, the
-- database says so immediately instead of producing a queue state that only
-- looks wrong three weeks later in a report.
--
-- Guarded with IF NOT EXISTS logic rather than plain ALTER so the migration
-- stays re-runnable, and so a second run never re-validates the whole table.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'call_ingest_jobs_attempts_nonneg'
                    AND conrelid = 'call_ingest_jobs'::regclass) THEN
    ALTER TABLE call_ingest_jobs
      ADD CONSTRAINT call_ingest_jobs_attempts_nonneg
      CHECK (asr_attempts >= 0 AND judge_attempts >= 0 AND retries >= 0);
  END IF;

  -- A lease is a THREE-column fact. A row that is in flight without a token
  -- can never be recovered (the sweep has nothing to blame and nothing to
  -- clear); a row that is not in flight but still carries a token is a lease
  -- nobody holds, which is exactly the state a half-applied terminal update
  -- would leave behind.
  --
  -- BOTH halves name all three columns. The first version's non-in-flight half
  -- omitted claimed_at, so 'evaluated' + claim_token NULL + claimed_at SET
  -- satisfied the constraint -- and that is precisely the shape a terminal
  -- update that forgot one line leaves behind. A constraint that permits the
  -- exact bug it exists to catch is decoration. Every release path
  -- (Mark evaluated / Mark judge failed / Mark ASR failed / Mark unscoreable /
  -- Dead-letter judge budget / the recovery sweep) already clears all three.
  --
  -- Re-runnability: the guard is on the DEFINITION, not just the name. A
  -- pre-release copy of this file created the looser two-column version under
  -- the same name, and `IF NOT EXISTS (conname)` would have kept it forever.
  -- 'claimed_at IS NULL' appears only in the tightened non-in-flight half --
  -- 'claimed_at IS NOT NULL' does not contain it -- so this test is exact.
  IF EXISTS (SELECT 1 FROM pg_constraint
              WHERE conname = 'call_ingest_jobs_lease_shape'
                AND conrelid = 'call_ingest_jobs'::regclass
                AND pg_get_constraintdef(oid) NOT LIKE '%claimed_at IS NULL%') THEN
    ALTER TABLE call_ingest_jobs DROP CONSTRAINT call_ingest_jobs_lease_shape;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'call_ingest_jobs_lease_shape'
                    AND conrelid = 'call_ingest_jobs'::regclass) THEN
    ALTER TABLE call_ingest_jobs
      ADD CONSTRAINT call_ingest_jobs_lease_shape
      CHECK (
        (status IN ('transcribing', 'evaluating')
           AND claim_token IS NOT NULL
           AND claimed_at  IS NOT NULL
           AND claim_until IS NOT NULL)
        OR
        (status NOT IN ('transcribing', 'evaluating')
           AND claim_token IS NULL
           AND claimed_at  IS NULL
           AND claim_until IS NULL)
      );
  END IF;
END $$;

-- PREFLIGHT for a database where a pre-release 012 already ran: the tightened
-- constraint is VALIDATED against existing rows, so a legacy row that is not in
-- flight but still carries claimed_at will make the ALTER above fail. Find them
-- first, and clear them -- they are leases nobody holds:
--
--   SELECT uniqueid, status, claim_token, claimed_at, claim_until
--     FROM call_ingest_jobs
--    WHERE status NOT IN ('transcribing','evaluating') AND claimed_at IS NOT NULL;
--
--   UPDATE call_ingest_jobs SET claimed_at = NULL, claim_token = NULL,
--                               claim_until = NULL
--    WHERE status NOT IN ('transcribing','evaluating')
--      AND (claimed_at IS NOT NULL OR claim_token IS NOT NULL
--           OR claim_until IS NOT NULL);

-- ---------------------------------------------------------------------------
-- 5 · indexes the claim and the recovery sweep ride on
-- ---------------------------------------------------------------------------
-- The claim filters on status + next_attempt_at and then orders by
-- (next_attempt_at, discovered_at, uniqueid). Indexing only the first two
-- columns still leaves a sort over every claimable row; carrying the whole
-- ordering means the planner can walk the index and stop at LIMIT n, which is
-- what keeps FOR UPDATE SKIP LOCKED from turning into a lock convoy once the
-- table has tens of thousands of rows.
--
-- `CREATE INDEX IF NOT EXISTS` matches on NAME, not on definition. 012 has
-- never been applied to any database, so there is no narrower index of this
-- name to replace; if a pre-release copy of this file ever did run somewhere,
-- DROP INDEX idx_call_ingest_jobs_claimable by hand first or the two-column
-- version silently survives.
CREATE INDEX IF NOT EXISTS idx_call_ingest_jobs_claimable
    ON call_ingest_jobs (status, next_attempt_at, discovered_at, uniqueid);

-- Recovery only ever looks at leased rows, and leased rows are a tiny minority.
CREATE INDEX IF NOT EXISTS idx_call_ingest_jobs_lease
    ON call_ingest_jobs (claim_until)
    WHERE claim_until IS NOT NULL;
