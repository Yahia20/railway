-- 017 — multi-request extraction, the Modal ASR batch log, re-judge limits,
--       and a retention policy that keeps the analysis after the words are gone.
--
-- Five separate decisions, one migration because they land together:
--
--   1. `interaction_requests` — a conversation can contain more than one thing
--      the customer wants, and until now only one survived.
--   2. `asr_runs` — transcription moves out of the worker and into a Modal
--      batch; a batch that leaves no record is a batch nobody can audit.
--   3. Re-judge limits — the reopen rule had no ceiling, and the judge bill is
--      the only line in this system that scales without one.
--   4. Retention — raw customer words are deleted after 90 days; every
--      conclusion drawn from them, and the id needed to fetch them again from
--      the source, is kept forever.
--   5. The idle window moves from 2 days to 3.
--
-- Idempotent throughout: IF NOT EXISTS, CREATE OR REPLACE, ADD COLUMN IF NOT
-- EXISTS. Running it twice changes nothing the second time.

-- LOCK DISCIPLINE. This runs against a live database: 01c inserts into
-- `interactions` roughly once a minute, and an ADD COLUMN needs an
-- AccessExclusiveLock. Without a timeout the two wait on each other and
-- Postgres kills one of them — which is exactly what happened on the first
-- attempt here (deadlock, whole migration rolled back).
--
-- lock_timeout makes that a fast, clean failure instead: we wait 5 seconds for
-- a gap between webhooks, and if there is none the migration aborts having
-- changed nothing and can simply be run again. Never raise this to 'wait
-- forever' on a table with live writers.
SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

-- ---------------------------------------------------------------------------
-- 1 · interaction_requests — every distinct thing the customer asked for
--
-- WHY A TABLE AND NOT MORE COLUMNS. A chat where the customer asks about a
-- Turkey package, a visa, and a domestic ticket is three commercial
-- opportunities in one thread. `interaction_analysis` holds exactly one
-- `service`, one `intent`, one `budget_amount`, so the second and third
-- requests had nowhere to go and were silently dropped — and the agent who
-- opened one deal instead of three looked correct in every report.
--
-- THE EXISTING COLUMNS DO NOT MOVE. `interaction_analysis.intent`, `.service`
-- and `.budget_amount` keep describing the PRIMARY request, so the report page,
-- v_real_asks, v_funnel and every dashboard query keep working untouched. This
-- table is additive: it answers "what else was in there".
--
-- EVIDENCE IS MANDATORY IN SPIRIT, NOT IN THE CONSTRAINT. A request with no
-- quote is a request the model invented; `evidence_valid` records the verdict
-- from the same verbatim check pass 2 has always used, and the reconciliation
-- view below counts only validated rows. A NOT NULL here would instead make the
-- whole write fail and lose the four good requests alongside the bad one.
CREATE TABLE IF NOT EXISTS interaction_requests (
  request_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  interaction_id uuid NOT NULL REFERENCES interactions(interaction_id) ON DELETE CASCADE,

  -- Position in the conversation, in the order pass 1 emitted them. `1` is the
  -- primary request and is the one mirrored into interaction_analysis.
  seq            int  NOT NULL,

  service        service_type,
  service_raw    text,
  intent         text,
  buying_stage   buying_stage,
  destination    text,
  date_start     date,
  date_end       date,
  nights         int,
  travelers_total int,
  budget_amount  numeric(12,2),
  budget_currency char(3),

  -- The fixed rule, not the model's opinion — same function 015 defined, fed
  -- this request's own fragment rather than the whole conversation.
  outcome        text,

  -- Verbatim customer words that put this request in the list.
  evidence       jsonb NOT NULL DEFAULT '[]'::jsonb,
  evidence_valid boolean,

  -- Which Bitrix deal, if any, a human actually opened for this request. NULL
  -- is the interesting value: it means nobody did.
  matched_bitrix_deal_id text,

  prompt_version text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (interaction_id, seq)
);
CREATE INDEX IF NOT EXISTS interaction_requests_interaction_idx
  ON interaction_requests (interaction_id);
CREATE INDEX IF NOT EXISTS interaction_requests_unmatched_idx
  ON interaction_requests (interaction_id) WHERE matched_bitrix_deal_id IS NULL;

COMMENT ON TABLE interaction_requests IS
  'One row per distinct thing the customer asked for in a conversation, '
  'extracted by pass 1. seq=1 is the primary request and is mirrored in '
  'interaction_analysis; the rest exist only here. matched_bitrix_deal_id NULL '
  'means no deal was opened for it.';

-- ---------------------------------------------------------------------------
-- 2 · interactions — every Bitrix deal id the thread carried, not just the first
--
-- WHY. 01c stores `external_deal_id` with COALESCE, so the first id to arrive
-- wins and is never replaced. That is right for the FK — a conversation belongs
-- to one deal — but it threw away the evidence needed to check our own work.
-- The reconciliation below compares what the model found against what the CRM
-- holds, and it cannot do that from a single id.
ALTER TABLE interactions
  ADD COLUMN IF NOT EXISTS external_deal_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
COMMENT ON COLUMN interactions.external_deal_ids IS
  'Every distinct Bitrix deal id seen on this thread''s messages, in arrival '
  'order. external_deal_id stays the first one and the FK. This is for '
  'reconciliation only — never join on it.';

-- Retention marker. A NULL here means the words are still there; a timestamp
-- means they were deleted on purpose. Without it a purged thread and a thread
-- that never had messages look identical, and every report would have to guess.
ALTER TABLE interactions
  ADD COLUMN IF NOT EXISTS content_purged_at timestamptz;
COMMENT ON COLUMN interactions.content_purged_at IS
  'When the raw words of this conversation were deleted by the retention job. '
  'NULL = still stored. The analysis and the source id are never purged.';

-- ---------------------------------------------------------------------------
-- 3 · asr_runs — one row per Modal batch
--
-- Transcription used to happen inside a request to the worker, where a failure
-- was a 500 somebody might notice. As a nightly batch on someone else's
-- machine it is invisible unless it writes down what it did. This is also the
-- only place the real cost of the ASR half will ever be visible: audio seconds
-- in, GPU seconds out.
CREATE TABLE IF NOT EXISTS asr_runs (
  run_id         text PRIMARY KEY,          -- Modal's function call id
  started_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz,
  status         text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'succeeded', 'failed', 'partial')),
  gpu            text,
  model_version  text,
  claimed        int NOT NULL DEFAULT 0,
  processed      int NOT NULL DEFAULT 0,
  failed         int NOT NULL DEFAULT 0,
  audio_seconds  numeric(12,1) NOT NULL DEFAULT 0,
  gpu_seconds    numeric(12,1) NOT NULL DEFAULT 0,
  -- Measured, not assumed. Every estimate in the cost model rests on RTFx and
  -- nobody has ever observed it; after the first run this column has.
  rtfx           numeric(8,2) GENERATED ALWAYS AS (
                   CASE WHEN gpu_seconds > 0 THEN audio_seconds / gpu_seconds END
                 ) STORED,
  est_cost_usd   numeric(10,4),
  error          text
);
CREATE INDEX IF NOT EXISTS asr_runs_started_idx ON asr_runs (started_at DESC);
COMMENT ON TABLE asr_runs IS
  'One row per Modal transcription batch. rtfx is computed, not configured — '
  'it is the only measurement that tells us whether the cost model was right.';

ALTER TABLE call_ingest_jobs
  ADD COLUMN IF NOT EXISTS asr_run_id text REFERENCES asr_runs(run_id);
COMMENT ON COLUMN call_ingest_jobs.asr_run_id IS
  'The batch that transcribed this call. NULL while it is still waiting.';

-- ---------------------------------------------------------------------------
-- 4 · Re-judge ceiling
--
-- THE RULE BEING CAPPED. A thread that gains a message after being judged is
-- judged again from scratch, against the fuller conversation. That is correct —
-- a score computed over half a conversation is worse than a late one — and it
-- is also the only unbounded cost in the system: nothing stopped a chatty
-- thread from being re-judged every time it woke up.
--
-- TWO CONDITIONS, BOTH REQUIRED, both enforced in 01d's ON CONFLICT:
--   judge_runs < 2                         at most one re-judge, ever
--   observed >= judged * 1.5               and only if the thread grew by half
--
-- Why 1.5 and not "any new message": one "شكرا" arriving three days later does
-- not change any module score, and re-judging for it pays full price for the
-- same answer.
ALTER TABLE chat_eval_jobs
  ADD COLUMN IF NOT EXISTS judge_runs int NOT NULL DEFAULT 0;
ALTER TABLE chat_eval_jobs
  ADD COLUMN IF NOT EXISTS judged_message_count int;
ALTER TABLE chat_eval_jobs
  ADD COLUMN IF NOT EXISTS observed_message_count int;

COMMENT ON COLUMN chat_eval_jobs.judge_runs IS
  'How many times this thread has been judged to completion. The reopen rule '
  'refuses to reopen at 2. Reset by nothing — a deliberate ceiling.';
COMMENT ON COLUMN chat_eval_jobs.judged_message_count IS
  'interactions.message_count as of the last completed judge. A reopen '
  'requires the thread to have grown 50% past this.';

-- Existing rows: they were judged once, and we do not know how big they were.
-- Seeding judged_message_count from the current count means the +50% test is
-- measured from today rather than reopening the whole backlog on the first tick.
UPDATE chat_eval_jobs j
   SET judge_runs = 1,
       judged_message_count = i.message_count,
       observed_message_count = i.message_count
  FROM interactions i
 WHERE i.interaction_id = j.interaction_id
   AND j.status IN ('evaluated', 'unscoreable')
   AND j.judged_message_count IS NULL;

-- ---------------------------------------------------------------------------
-- 5 · The idle window moves to 3 days
--
-- Defined here and nowhere else, exactly as 016 established. Longer means a
-- thread that wakes up briefly settles down before it costs a second judge
-- call, and it means the score is computed over a conversation that is more
-- likely finished. The cost is one day of delay on coaching feedback, which
-- this pipeline was never fast enough to matter for.
CREATE OR REPLACE VIEW v_chat_eval_due AS
SELECT
  i.interaction_id,
  i.external_id,
  i.external_source,
  i.channel,
  i.message_count,
  i.customer_message_count,
  i.agent_message_count,
  m.last_message_at,
  round(extract(epoch FROM (now() - m.last_message_at)) / 86400.0, 2) AS idle_days
FROM interactions i
JOIN LATERAL (
  SELECT max(sent_at) AS last_message_at FROM chat_messages c
  WHERE c.interaction_id = i.interaction_id
) m ON true
WHERE i.external_source = 'bitrix_chat_api'
  AND m.last_message_at IS NOT NULL
  AND m.last_message_at < now() - interval '3 days'
  AND i.message_count >= 2
  AND i.agent_message_count > 0
  AND i.customer_message_count > 0
  -- Purged threads have no words left to judge. They keep their old score.
  AND i.content_purged_at IS NULL;
COMMENT ON VIEW v_chat_eval_due IS
  'Chat threads from the production API, silent for 3 days (was 2 until 017) '
  'and worth grading. The idle window is defined here only; workflow 01d and '
  'any backfill both read it from this view.';

-- ---------------------------------------------------------------------------
-- 6 · v_request_reconciliation — what the model found vs what the CRM holds
--
-- THE POINT OF THE WHOLE CHANGE. Bitrix is not the source of truth for what a
-- customer asked for; it is a record of what an agent got around to writing
-- down. The gap between the two is revenue nobody logged, and this view is the
-- gap, per conversation.
--
-- ONLY EVIDENCE-BACKED REQUESTS COUNT. `evidence_valid IS NOT FALSE` keeps
-- rows the validator passed and rows it could not check, and drops the ones it
-- caught inventing a quote — a fabricated request would otherwise show up as a
-- missed opportunity and send somebody chasing a customer who never asked.
CREATE OR REPLACE VIEW v_request_reconciliation AS
SELECT
  i.interaction_id,
  i.external_id,
  i.channel,
  i.started_at,
  i.external_deal_id                                   AS primary_bitrix_deal,
  jsonb_array_length(i.external_deal_ids)              AS bitrix_deal_count,
  count(r.request_id) FILTER (WHERE r.evidence_valid IS NOT FALSE) AS ai_request_count,
  count(r.request_id) FILTER (WHERE r.matched_bitrix_deal_id IS NULL
                                AND r.evidence_valid IS NOT FALSE) AS unlogged_request_count,
  CASE
    WHEN count(r.request_id) = 0                              THEN 'not_analysed'
    WHEN count(r.request_id) FILTER (WHERE r.evidence_valid IS NOT FALSE)
         > GREATEST(jsonb_array_length(i.external_deal_ids), 1) THEN 'crm_missing_deals'
    WHEN count(r.request_id) FILTER (WHERE r.evidence_valid IS NOT FALSE)
         < jsonb_array_length(i.external_deal_ids)             THEN 'crm_has_extra'
    ELSE 'agreed'
  END                                                  AS verdict,
  sum(r.budget_amount) FILTER (WHERE r.matched_bitrix_deal_id IS NULL
                                 AND r.evidence_valid IS NOT FALSE) AS unlogged_budget
FROM interactions i
LEFT JOIN interaction_requests r ON r.interaction_id = i.interaction_id
GROUP BY i.interaction_id, i.external_id, i.channel, i.started_at,
         i.external_deal_id, i.external_deal_ids;
COMMENT ON VIEW v_request_reconciliation IS
  'Per conversation: how many requests the model found with evidence, how many '
  'deals Bitrix holds, and the gap. crm_missing_deals is the finding this '
  'project exists to produce — a request nobody opened a deal for.';

-- ---------------------------------------------------------------------------
-- 6b · model_calls gains the two facts that decide what a call actually cost
--
-- The table has existed since 006 and nothing has ever written to it. Two
-- columns were missing before it could: the cache split (a cached prompt token
-- is priced 31x below a fresh one, and the static prompt is ~15k tokens, so
-- pricing the prompt at one rate overstates a chat by about 5x), and whether
-- the call landed in DeepSeek's peak window, where everything costs double.
ALTER TABLE model_calls ADD COLUMN IF NOT EXISTS cached_tokens  int;
ALTER TABLE model_calls ADD COLUMN IF NOT EXISTS priced_at_peak boolean;
COMMENT ON COLUMN model_calls.cached_tokens IS
  'Prompt tokens served from DeepSeek prefix cache. prompt_tokens minus this '
  'is what was charged at the full input rate.';
COMMENT ON COLUMN model_calls.priced_at_peak IS
  'True if the call ran inside 01:00-04:00 or 06:00-10:00 UTC Mon-Fri, where '
  'every rate doubles. The nightly schedule exists to keep this false.';

-- ---------------------------------------------------------------------------
-- 7 · purge_raw_content — delete the words, keep the conclusions
--
-- WHAT IS DELETED: message bodies, transcript text, the raw webhook payloads,
-- and the model's raw_response blob. All four are customer words or copies of
-- them, and PDPL says do not keep personal data longer than the purpose needs.
--
-- WHAT IS NEVER TOUCHED: interactions (with external_id and external_deal_id),
-- interaction_analysis, agent_evaluations, interaction_requests, follow_ups,
-- alerts. Every number in every report survives, and the id needed to fetch the
-- original from Bitrix or Drive survives with it.
--
-- TWO WINDOWS, ON PURPOSE. Chat text is reproducible — Bitrix still has it.
-- A call recording is not reliably reproducible: if Drive's own retention
-- deletes the WAV after we deleted our transcript, the conversation is gone
-- from the world. So call text is kept four times longer, and the caller can
-- widen it further without touching this function.
CREATE OR REPLACE FUNCTION purge_raw_content(
  p_chat_days int DEFAULT 90,
  p_call_days int DEFAULT 365,
  p_dry_run   boolean DEFAULT true
) RETURNS TABLE (what text, rows_affected bigint)
LANGUAGE plpgsql AS $fn$
DECLARE
  chat_cut timestamptz := now() - make_interval(days => p_chat_days);
  call_cut timestamptz := now() - make_interval(days => p_call_days);
  n bigint;
BEGIN
  -- Chat message bodies.
  IF p_dry_run THEN
    SELECT count(*) INTO n FROM chat_messages m
      JOIN interactions i USING (interaction_id)
     WHERE i.started_at < chat_cut AND i.content_purged_at IS NULL;
  ELSE
    WITH doomed AS (
      DELETE FROM chat_messages m
       USING interactions i
       WHERE i.interaction_id = m.interaction_id
         AND i.started_at < chat_cut
         AND i.content_purged_at IS NULL
      RETURNING m.message_id
    ) SELECT count(*) INTO n FROM doomed;
  END IF;
  what := 'chat_messages deleted'; rows_affected := n; RETURN NEXT;

  -- Transcript text. The row stays: asr_confidence, duration and diarization
  -- are quality measurements about our own pipeline, not customer content.
  IF p_dry_run THEN
    SELECT count(*) INTO n FROM transcripts t
      JOIN interactions i USING (interaction_id)
     WHERE i.started_at < call_cut AND t.full_text IS NOT NULL;
  ELSE
    WITH doomed AS (
      UPDATE transcripts t
         SET full_text = NULL, segments = NULL
        FROM interactions i
       WHERE i.interaction_id = t.interaction_id
         AND i.started_at < call_cut
         AND t.full_text IS NOT NULL
      RETURNING t.transcript_id
    ) SELECT count(*) INTO n FROM doomed;
  END IF;
  what := 'transcripts blanked'; rows_affected := n; RETURN NEXT;

  -- Raw webhook payloads: a byte-for-byte copy of what is already parsed.
  IF p_dry_run THEN
    SELECT count(*) INTO n FROM raw_events WHERE received_at < chat_cut;
  ELSE
    WITH doomed AS (
      DELETE FROM raw_events WHERE received_at < chat_cut RETURNING raw_id
    ) SELECT count(*) INTO n FROM doomed;
  END IF;
  what := 'raw_events deleted'; rows_affected := n; RETURN NEXT;

  -- The model's own transcript of its reasoning, which quotes the customer.
  -- Every field parsed out of it is already in its own column.
  IF p_dry_run THEN
    SELECT count(*) INTO n FROM interaction_analysis a
      JOIN interactions i USING (interaction_id)
     WHERE i.started_at < chat_cut AND a.raw_response IS NOT NULL;
  ELSE
    WITH doomed AS (
      UPDATE interaction_analysis a SET raw_response = NULL
        FROM interactions i
       WHERE i.interaction_id = a.interaction_id
         AND i.started_at < chat_cut
         AND a.raw_response IS NOT NULL
      RETURNING a.analysis_id
    ) SELECT count(*) INTO n FROM doomed;
  END IF;
  what := 'pass1 raw_response blanked'; rows_affected := n; RETURN NEXT;

  IF p_dry_run THEN
    SELECT count(*) INTO n FROM agent_evaluations e
      JOIN interactions i USING (interaction_id)
     WHERE i.started_at < chat_cut AND e.raw_response IS NOT NULL;
  ELSE
    WITH doomed AS (
      UPDATE agent_evaluations e SET raw_response = NULL
        FROM interactions i
       WHERE i.interaction_id = e.interaction_id
         AND i.started_at < chat_cut
         AND e.raw_response IS NOT NULL
      RETURNING e.evaluation_id
    ) SELECT count(*) INTO n FROM doomed;
  END IF;
  what := 'pass2 raw_response blanked'; rows_affected := n; RETURN NEXT;

  -- Stamp last. A thread is only marked purged once its words are actually
  -- gone, so a crash halfway through leaves it eligible for the next run
  -- instead of marked done with content still present.
  IF NOT p_dry_run THEN
    WITH stamped AS (
      UPDATE interactions SET content_purged_at = now()
       WHERE started_at < chat_cut AND content_purged_at IS NULL
      RETURNING interaction_id
    ) SELECT count(*) INTO n FROM stamped;
    what := 'interactions stamped'; rows_affected := n; RETURN NEXT;
  END IF;
END
$fn$;
COMMENT ON FUNCTION purge_raw_content(int, int, boolean) IS
  'Deletes raw customer words older than the given windows and stamps '
  'interactions.content_purged_at. Analysis, scores, requests and source ids '
  'are never touched. Dry run by default — pass p_dry_run => false to apply.';

COMMIT;
