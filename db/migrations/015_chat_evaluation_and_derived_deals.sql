-- 015 — scoring the stored chats, and deals assessed from the conversation.
--
-- TWO THINGS, one migration, because the second depends on the first: a deal
-- can only be assessed from a conversation that has been analysed.
--
-- 1 · chat_eval_jobs — the same leased state machine 012 gave the calls, for
--     chat threads. Workflow 01c stores and stops; something has to decide
--     WHEN a thread is finished enough to grade, claim it exactly once, and
--     survive a worker dying mid-judge.
--
-- 2 · deals gains an ASSESSED half. The CRM half (stage_id, amount, dates) is
--     what Bitrix says. The assessed half is what the conversation actually
--     shows. They are kept in separate columns and never merged, because the
--     reason this exists is that the two disagree: the agent does not always
--     move the stage, so `stage_id` says "Assigned" on a deal the customer
--     walked away from three weeks ago.
--
-- Idempotent: IF NOT EXISTS everywhere, guarded constraint adds, CREATE OR
-- REPLACE VIEW. Safe to re-run.
--
-- ATOMIC. One transaction: a half-applied 015 would leave a jobs table with no
-- claim index and a deals table with provenance columns nothing can populate.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1 · chat_eval_jobs — one row per thread that is due for scoring.
--
-- WHY A JOBS TABLE AND NOT A QUERY. "Threads with no analysis whose last
-- message is older than the idle window" is a perfectly good SELECT, and it is
-- also the wrong thing to drive a judge from: it has no memory. A thread that
-- fails the contract twice would be re-judged on every tick forever, a thread
-- being judged right now would be picked up by the next tick as well, and a
-- thread the model refused as unscoreable would be paid for again every 30
-- minutes. The table is where "we already tried, and here is what happened"
-- lives.
--
-- KEYED BY interaction_id. One thread, one job, for the life of the thread —
-- unlike the calls, where the natural key is the recording. ON DELETE CASCADE
-- so deleting a conversation cannot leave a job pointing at nothing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_eval_jobs (
  interaction_id  uuid PRIMARY KEY
                  REFERENCES interactions(interaction_id) ON DELETE CASCADE,

  status          text NOT NULL DEFAULT 'pending',

  -- The moment that made this thread eligible: the last message we held when
  -- the job was registered. Kept so a thread that comes back to life can be
  -- detected — see `idle_through` below and section 1a.
  idle_through    timestamptz NOT NULL,

  -- Lease. Identical shape to call_ingest_jobs so the two workflows can be
  -- read side by side and reviewed against one another.
  claimed_at      timestamptz,
  claim_until     timestamptz,
  claim_token     uuid,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  judge_attempts  int NOT NULL DEFAULT 0,
  last_error      text,

  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chat_eval_jobs_status_ck') THEN
    ALTER TABLE chat_eval_jobs ADD CONSTRAINT chat_eval_jobs_status_ck
      CHECK (status IN (
        'pending',       -- registered, waiting for its turn
        'evaluating',    -- claimed, a judge call is in flight
        'evaluated',     -- both passes stored. Terminal, and the happy path.
        'unscoreable',   -- nothing to grade: bot-only, no customer turn, or
                         -- below the scoring minimum. TERMINAL — retrying it
                         -- re-asks a model that already answered and can only
                         -- manufacture a score.
        'judge_failed',  -- retryable: the judge was unreachable, rate limited,
                         -- or broke its own contract after the re-ask.
        'dead_letter'    -- out of attempts. Terminal, and visible.
      ));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chat_eval_jobs_attempts_ck') THEN
    ALTER TABLE chat_eval_jobs ADD CONSTRAINT chat_eval_jobs_attempts_ck
      CHECK (judge_attempts >= 0);
  END IF;

  -- A claim is a token AND a deadline, never one without the other: a token
  -- with no deadline is a lease nothing can reclaim, and a deadline with no
  -- token is a lease nothing can fence a write against.
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chat_eval_jobs_lease_ck') THEN
    ALTER TABLE chat_eval_jobs ADD CONSTRAINT chat_eval_jobs_lease_ck
      CHECK ((claim_token IS NULL) = (claim_until IS NULL));
  END IF;
END
$do$;

-- Partial, on exactly the predicate the claim uses, so claiming stays an index
-- scan as the terminal rows accumulate.
CREATE INDEX IF NOT EXISTS idx_chat_eval_jobs_claimable
  ON chat_eval_jobs (next_attempt_at, idle_through)
  WHERE status IN ('pending', 'judge_failed');

CREATE INDEX IF NOT EXISTS idx_chat_eval_jobs_lease
  ON chat_eval_jobs (claim_until)
  WHERE status = 'evaluating';

CREATE INDEX IF NOT EXISTS idx_chat_eval_jobs_status
  ON chat_eval_jobs (status);

DROP TRIGGER IF EXISTS t_chat_eval_jobs_updated ON chat_eval_jobs;
CREATE TRIGGER t_chat_eval_jobs_updated BEFORE UPDATE ON chat_eval_jobs
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE chat_eval_jobs IS
  'One row per chat thread due for scoring. Leased exactly like '
  'call_ingest_jobs (012). Registered only when a thread has been silent for '
  'CHAT_IDLE_DAYS; see v_chat_eval_due.';
COMMENT ON COLUMN chat_eval_jobs.idle_through IS
  'max(sent_at) at the moment the job was registered. If the thread gains a '
  'newer message the job is reopened and re-judged against the fuller thread — '
  'a score computed over half a conversation is worse than a late one.';

-- ---------------------------------------------------------------------------
-- 1a · v_chat_eval_due — which threads are ready, defined ONCE.
--
-- THE IDLE RULE LIVES HERE AND NOWHERE ELSE. A chat has no hangup: the only
-- signal that it is over is that nobody has said anything for a while. Two
-- days, by decision, not by guess — long enough that a customer replying the
-- next morning is still part of the same conversation, short enough that
-- coaching arrives while the agent remembers the thread. The window is a
-- setting on the view so changing it can never leave the workflow and the
-- backfill disagreeing about what "finished" means.
--
-- EXCLUSIONS, and why each is a WHERE clause rather than something the judge
-- discovers after it has been paid:
--   * threads with no agent turn   — nothing to grade, it is a bot log
--   * threads with no customer turn — a labelling fault at the source
--   * threads with fewer than 2 messages — not a conversation
-- The worker's /chats/prepare applies the first two again on the real message
-- rows; this is the cheap filter, that is the authoritative one.
-- ---------------------------------------------------------------------------
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
WHERE i.external_source IN ('bitrix_chat_api', 'bitrix')
  AND m.last_message_at IS NOT NULL
  AND m.last_message_at < now() - interval '2 days'
  AND i.message_count >= 2
  AND i.agent_message_count > 0
  AND i.customer_message_count > 0;

COMMENT ON VIEW v_chat_eval_due IS
  'Chat threads silent for 2 days and worth grading. The idle window is '
  'defined here only; workflow 01d and any backfill both read this view.';

-- ---------------------------------------------------------------------------
-- 2 · deals — the assessed half.
--
-- WHY NOT TRUST THE CRM STAGE. `stage_id` is set by hand by the agent who
-- owns the deal, and the agent who forgets to move a deal to "lost" is the
-- same agent whose conversations we are grading. The funnel built on that
-- column reports what people remembered to click.
--
-- So the outcome is READ FROM THE CONVERSATION instead, by the same pass-1
-- extraction that already produces buying_stage and objections. The CRM
-- columns are left exactly as they are: this migration adds columns, it
-- overwrites nothing, and any report may still ask "what does Bitrix say"
-- beside "what did the conversation show".
--
-- PROVENANCE IS NOT OPTIONAL. `origin` says who created the ROW; the ai_*
-- columns say who wrote the ASSESSMENT. A row created from a conversation and
-- a row pulled from the CRM must never be indistinguishable, or the next
-- person to read this table will average the two together.
-- ---------------------------------------------------------------------------
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS origin              text NOT NULL DEFAULT 'bitrix',
  ADD COLUMN IF NOT EXISTS ai_outcome          text,
  ADD COLUMN IF NOT EXISTS ai_outcome_reason   text,
  ADD COLUMN IF NOT EXISTS ai_stage_reached    text,
  ADD COLUMN IF NOT EXISTS ai_service          service_type,
  ADD COLUMN IF NOT EXISTS ai_lead_temp        lead_temperature,
  ADD COLUMN IF NOT EXISTS ai_budget_amount    numeric(14,2),
  ADD COLUMN IF NOT EXISTS ai_currency         char(3),
  ADD COLUMN IF NOT EXISTS ai_confidence       numeric(3,2),
  ADD COLUMN IF NOT EXISTS ai_assessed_at      timestamptz,
  ADD COLUMN IF NOT EXISTS ai_conversation_at  timestamptz,
  ADD COLUMN IF NOT EXISTS ai_assessed_from    uuid REFERENCES interactions(interaction_id),
  ADD COLUMN IF NOT EXISTS ai_prompt_version   text,
  ADD COLUMN IF NOT EXISTS ai_model            text;

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'deals_origin_ck') THEN
    ALTER TABLE deals ADD CONSTRAINT deals_origin_ck
      CHECK (origin IN ('bitrix', 'ai_derived'));
  END IF;

  -- 'unknown' is a real answer and must be storable. A deal whose conversation
  -- never reached a decision is not a lost deal, and forcing it into one of
  -- the three real outcomes is exactly the distortion this table exists to
  -- remove from the funnel.
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'deals_ai_outcome_ck') THEN
    ALTER TABLE deals ADD CONSTRAINT deals_ai_outcome_ck
      CHECK (ai_outcome IS NULL OR ai_outcome IN (
        'won',              -- the customer committed: booked, paid, confirmed
        'lost',             -- explicitly declined, or went elsewhere
        'in_progress',      -- live and moving; a quote is out, or dates are
                            -- being agreed
        'no_opportunity',   -- never was a deal: wrong number, an existing
                            -- booking's admin, a service we do not sell
        'unknown'           -- the conversation does not say
      ));
  END IF;

  -- An assessment is a claim about a moment: without knowing which
  -- conversation and when, a stale outcome cannot be told from a current one.
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'deals_ai_provenance_ck') THEN
    ALTER TABLE deals ADD CONSTRAINT deals_ai_provenance_ck
      CHECK (ai_outcome IS NULL
             OR (ai_assessed_at IS NOT NULL AND ai_assessed_from IS NOT NULL));
  END IF;
END
$do$;

CREATE INDEX IF NOT EXISTS idx_deals_ai_outcome
  ON deals (ai_outcome) WHERE ai_outcome IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_deals_origin ON deals (origin);

COMMENT ON COLUMN deals.origin IS
  'bitrix = the row came from the CRM. ai_derived = the row was created from a '
  'conversation because the CRM deal had not been imported. Either way '
  'bitrix_deal_id is the real Bitrix id — it is the natural key, not a claim.';
COMMENT ON COLUMN deals.ai_outcome IS
  'What the CONVERSATION shows, not what the agent clicked. Deliberately '
  'separate from stage_id / stage_semantic, which stay untouched: the two '
  'disagreeing is the finding, not a conflict to resolve.';
COMMENT ON COLUMN deals.ai_assessed_from IS
  'The interaction this assessment was read from — the latest analysed '
  'conversation about this deal. A deal can have many conversations; the most '
  'recent one is the only one whose outcome is current.';

-- ---------------------------------------------------------------------------
-- 2a · deal_outcome_from_analysis — the mapping, defined ONCE.
--
-- THE MODEL OBSERVES; THIS FUNCTION DECIDES. Pass 1 is never asked "did this
-- deal close" — that is a conclusion, and a model asked for a conclusion will
-- produce a confident one from a conversation that does not support it. It is
-- asked only what it can see in the text: is this a real inquiry, and what
-- buying stage did the customer reach. The outcome is then a fixed rule over
-- those two answers, which means it is reproducible, auditable, and changes
-- only when somebody edits this function.
--
-- Same principle as judge.py discarding the model's own final_score and
-- recomputing it: the arithmetic belongs to the engine.
--
-- `no_opportunity` OUTRANKS EVERYTHING. A wrong number, a courier asking for
-- directions, or somebody chasing an existing booking is not a lost sale, and
-- counting it as one is how a funnel acquires a fake denominator. pass 1 says
-- so directly through real_ask.is_real_inquiry.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION deal_outcome_from_analysis(p_raw jsonb)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
  SELECT CASE
    WHEN p_raw IS NULL THEN NULL
    -- Explicitly false only. A missing key means pass 1 did not answer, which
    -- is 'unknown' further down, NOT 'this was not a real inquiry'.
    WHEN (p_raw->'real_ask'->>'is_real_inquiry') = 'false' THEN 'no_opportunity'
    WHEN (p_raw->'commercial'->>'buying_stage') = 'purchased' THEN 'won'
    WHEN (p_raw->'commercial'->>'buying_stage') = 'lost'      THEN 'lost'
    WHEN (p_raw->'commercial'->>'buying_stage')
         IN ('awareness', 'consideration', 'decision')        THEN 'in_progress'
    ELSE 'unknown'
  END
$fn$;

COMMENT ON FUNCTION deal_outcome_from_analysis(jsonb) IS
  'Maps a pass-1 raw_response to a deal outcome. The model supplies the two '
  'observations (is_real_inquiry, buying_stage); this fixed rule turns them '
  'into won/lost/in_progress/no_opportunity/unknown. Never ask the model for '
  'the outcome directly.';

-- ---------------------------------------------------------------------------
-- 3 · v_deal_conversations — every conversation about a deal, chat and call
--     together, with its analysis and its score.
--
-- This is the join the ERD makes look easy and that everything below needs:
-- a deal is reached from an interaction by external_deal_id (the id as the
-- source sent it) OR by deal_id (the resolved FK), because ingest deliberately
-- does not create a deals row and most interactions therefore only have the
-- former.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_deal_conversations AS
SELECT
  coalesce(d.bitrix_deal_id, i.external_deal_id) AS bitrix_deal_id,
  d.deal_id,
  i.interaction_id,
  i.customer_id,
  i.customer_phone_e164,
  i.external_contact_id,
  i.agent_id,
  CASE WHEN i.external_source = 'asterisk_drive' THEN 'call' ELSE 'chat' END AS medium,
  i.channel,
  i.started_at,
  i.ended_at,
  i.message_count,
  a.analysis_id,
  a.service,
  a.buying_stage,
  a.lead_temp,
  a.budget_amount,
  a.budget_currency,
  a.summary_ar,
  a.confidence      AS analysis_confidence,
  e.final_score,
  e.performance_level,
  e.stage_reached,
  e.contract_status,
  e.gradeable
FROM interactions i
LEFT JOIN deals d
       ON d.bitrix_deal_id = i.external_deal_id
LEFT JOIN interaction_analysis a USING (interaction_id)
LEFT JOIN agent_evaluations   e USING (interaction_id)
WHERE i.external_deal_id IS NOT NULL OR i.deal_id IS NOT NULL;

COMMENT ON VIEW v_deal_conversations IS
  'One row per conversation about a deal, chats and calls together. Joins on '
  'external_deal_id because ingest never creates a deals row, so deal_id is '
  'null on almost every interaction.';

-- ---------------------------------------------------------------------------
-- 4 · v_customer_deals — per customer, one row per deal, with its outcome.
--
-- IDENTITY. Keyed on customer_id where RESOLVE has linked one, and on the
-- phone otherwise, so the view is useful BEFORE identity resolution has run
-- rather than empty until it does. `identity_basis` says which it used, so
-- nobody reads a phone-grouped row as a resolved customer.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_customer_deals AS
SELECT
  coalesce(v.customer_id::text, v.customer_phone_e164, 'contact:' || v.external_contact_id)
                                                        AS customer_key,
  CASE WHEN v.customer_id IS NOT NULL THEN 'customer_id'
       WHEN v.customer_phone_e164 IS NOT NULL THEN 'phone'
       ELSE 'bitrix_contact' END                        AS identity_basis,
  v.customer_id,
  v.customer_phone_e164,
  v.bitrix_deal_id,
  d.ai_outcome,
  d.ai_outcome_reason,
  d.ai_service,
  d.ai_lead_temp,
  d.ai_budget_amount,
  d.ai_currency,
  d.ai_assessed_at,
  d.stage_id                                            AS crm_stage,
  d.amount                                              AS crm_amount,
  count(*)                                              AS conversations,
  count(*) FILTER (WHERE v.medium = 'chat')             AS chat_conversations,
  count(*) FILTER (WHERE v.medium = 'call')             AS call_conversations,
  count(v.analysis_id)                                  AS conversations_analysed,
  min(v.started_at)                                     AS first_contact_at,
  max(coalesce(v.ended_at, v.started_at))               AS last_contact_at,
  -- Only usable scores are averaged. 014 defines usable once; restating the
  -- rule here is how the old scorecard ended up counting rows it then skipped.
  round(avg(v.final_score) FILTER (WHERE eval_score_is_usable(
          v.contract_status, v.gradeable, v.final_score)), 1) AS avg_agent_score,
  count(*) FILTER (WHERE eval_score_is_usable(
          v.contract_status, v.gradeable, v.final_score))     AS scored_conversations
FROM v_deal_conversations v
LEFT JOIN deals d ON d.bitrix_deal_id = v.bitrix_deal_id
WHERE v.bitrix_deal_id IS NOT NULL
GROUP BY
  1, 2, v.customer_id, v.customer_phone_e164, v.bitrix_deal_id,
  d.ai_outcome, d.ai_outcome_reason, d.ai_service, d.ai_lead_temp,
  d.ai_budget_amount, d.ai_currency, d.ai_assessed_at, d.stage_id, d.amount;

COMMENT ON VIEW v_customer_deals IS
  'One row per (customer, deal): how many chats and calls it took, whether it '
  'was analysed, and what the conversation says the outcome was. Grouped by '
  'customer_id once RESOLVE has run, by phone before that.';

-- ---------------------------------------------------------------------------
-- 5 · v_customer_summary — the Customer 360 line itself.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_customer_summary AS
SELECT
  customer_key,
  max(identity_basis)                                       AS identity_basis,
  max(customer_id::text)::uuid                              AS customer_id,
  max(customer_phone_e164)                                  AS phone,
  count(*)                                                  AS deals,
  count(*) FILTER (WHERE ai_outcome = 'won')                AS deals_won,
  count(*) FILTER (WHERE ai_outcome = 'lost')               AS deals_lost,
  count(*) FILTER (WHERE ai_outcome = 'in_progress')        AS deals_in_progress,
  count(*) FILTER (WHERE ai_outcome = 'no_opportunity')     AS deals_no_opportunity,
  count(*) FILTER (WHERE ai_outcome IS NULL)                AS deals_not_assessed,
  sum(conversations)                                        AS conversations,
  sum(chat_conversations)                                   AS chat_conversations,
  sum(call_conversations)                                   AS call_conversations,
  -- The customer's STATED BUDGET on won deals, not booked revenue: pass 1
  -- reads what the customer said they wanted to spend. Real revenue lives in
  -- deals.amount, which comes from the CRM.
  sum(ai_budget_amount) FILTER (WHERE ai_outcome = 'won')   AS won_stated_budget,
  min(first_contact_at)                                     AS first_contact_at,
  max(last_contact_at)                                      AS last_contact_at,
  round(avg(avg_agent_score) FILTER (WHERE avg_agent_score IS NOT NULL), 1)
                                                            AS avg_agent_score
FROM v_customer_deals
GROUP BY customer_key;

COMMENT ON VIEW v_customer_summary IS
  'One line per customer: how many deals, won/lost/open, over how many chats '
  'and calls, and how well they were served. Outcomes are read from the '
  'conversations, not from the CRM stage.';

COMMIT;
