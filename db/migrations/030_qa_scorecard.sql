-- 030 — the QA scorecard: where its results live, its switch, its queue, and
-- its own spending gate.
--
-- WHAT THIS IS. The 15-item chat check from the company's QA sheet, approved on
-- 2026-10-02 (artifact "تقييم جودة الموظفين", 50 chats) and trialled on the 297
-- chats of the week 2026-09-27 → 10-04. The code is app/qa; workflow 10 claims
-- a chat here, asks the worker (POST /qa/evaluate), and stores the answer.
--
-- WHY A TABLE OF ITS OWN and not agent_evaluations. That table is v7's: five
-- modules, m1..m5 columns, `rubric_version 1.0.0`, and every view on it
-- (v_agent_scorecard, v_usable_evaluations, …) means that rubric. Writing a
-- different rubric's numbers into it would make every existing report average
-- two scales together without saying so.
--
-- WHY ITS OWN GATE. v_pipeline_gate's `may_run` is false whenever
-- provider_budgets.enabled is false for deepseek — and that switch was turned
-- off by hand on 2026-09-15 precisely to keep the OLD rubric (01d) from
-- running. Reusing it would tie the new method to the old one's off switch.
-- v_qa_gate keeps every money rule — monthly cap, balance known and available,
-- fail closed on a stale probe — and swaps only the on/off switch for
-- qa_config.mode. QA calls land in model_calls with provider 'deepseek', so
-- they count against the same monthly cap as everything else.

BEGIN;

SET lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS qa_config (
  key        text PRIMARY KEY,
  value      text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO qa_config (key, value) VALUES
  ('mode',  'off'),          -- 'on' lets workflow 10 spend; 'off' is a pause, nothing is lost
  ('since', '2026-09-27'),   -- the first day (Riyadh) that is graded at all
  ('batch', '5')             -- chats per 10-minute tick
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS qa_evaluations (
  interaction_id uuid PRIMARY KEY REFERENCES interactions (interaction_id) ON DELETE CASCADE,
  status         text NOT NULL
                 CHECK (status IN ('claimed', 'scored', 'not_gradeable', 'failed')),
  agent_id       uuid REFERENCES agents (agent_id),
  prompt_version text,
  model          text,
  runs           smallint,
  score          numeric(5, 1),
  critical       smallint[] NOT NULL DEFAULT '{}',
  -- Per question: {"ans": yes|no|na, "why": ..., "quotes": [...]}. The quotes
  -- are customer and agent text, so workflow 10 blanks this column once the
  -- conversation itself is purged (90 days); score and categories stay.
  items          jsonb,
  categories     jsonb,
  cost_usd       numeric(10, 6),
  reason         text,
  attempts       smallint NOT NULL DEFAULT 0,
  claimed_at     timestamptz,
  evaluated_at   timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT qa_scored_is_complete
    CHECK (status <> 'scored' OR (prompt_version IS NOT NULL AND categories IS NOT NULL))
);

-- The claim fence, as in workflow 09: a claim writes a fresh token and only the
-- run holding that token may finish the row, so an answer that arrives after
-- its claim was taken over by a later tick matches nothing.
ALTER TABLE qa_evaluations ADD COLUMN IF NOT EXISTS claim_token uuid;

CREATE INDEX IF NOT EXISTS qa_evaluations_agent_idx
  ON qa_evaluations (agent_id) WHERE status = 'scored';

-- Chats waiting to be graded. The filter is the trial's, unchanged: a human
-- agent who is not a bot, both sides wrote at least twice, 4-60 lines, and
-- quiet for 3 days — item 27 ("came back as promised within 3 days") cannot be
-- judged before 3 days have passed. A claim older than 30 minutes is a crashed
-- run and is taken again; a failure is retried twice more, an hour apart.
CREATE OR REPLACE VIEW v_qa_due AS
SELECT i.interaction_id, i.started_at
FROM interactions i
JOIN agents a USING (agent_id)
LEFT JOIN qa_evaluations q USING (interaction_id)
WHERE i.external_source = 'bitrix_chat_api'
  AND NOT a.is_bot AND a.is_active
  AND i.customer_message_count >= 2
  AND i.agent_message_count >= 2
  AND i.message_count BETWEEN 4 AND 60
  AND i.started_at >= ((SELECT value FROM qa_config WHERE key = 'since')::date::timestamp
                       AT TIME ZONE 'Asia/Riyadh')
  AND i.ended_at < now() - interval '3 days'
  AND i.content_purged_at IS NULL
  AND (q.interaction_id IS NULL
       OR (q.status = 'claimed' AND q.claimed_at < now() - interval '30 minutes')
       OR (q.status = 'failed' AND q.attempts < 3 AND q.updated_at < now() - interval '1 hour'));

CREATE OR REPLACE VIEW v_qa_gate AS
SELECT coalesce(c.value, 'off')          AS mode,
       s.spend_mtd_usd,
       s.monthly_cap_usd,
       st.balance_usd,
       st.checked_at,
       CASE
         WHEN coalesce(c.value, 'off') <> 'on'                      THEN false
         WHEN s.hard_stop AND s.over_cap                            THEN false
         WHEN st.provider IS NULL OR st.available IS DISTINCT FROM true THEN false
         WHEN st.checked_at < now() - interval '1 hour'             THEN false
         ELSE true
       END AS may_run,
       CASE
         WHEN coalesce(c.value, 'off') <> 'on'                      THEN 'qa_config.mode is off'
         WHEN s.hard_stop AND s.over_cap
           THEN format('monthly cap reached: %s of %s USD spent',
                       round(s.spend_mtd_usd, 2), s.monthly_cap_usd)
         WHEN st.provider IS NULL                                   THEN 'no preflight has run yet - balance unknown'
         WHEN st.available IS DISTINCT FROM true
           THEN coalesce(st.reason, 'provider reports no available balance')
         WHEN st.checked_at < now() - interval '1 hour'             THEN 'balance check is older than an hour'
       END AS reason
FROM v_spend_mtd s
LEFT JOIN provider_status st ON st.provider = s.provider
LEFT JOIN qa_config c ON c.key = 'mode'
WHERE s.provider = 'deepseek';

COMMIT;
