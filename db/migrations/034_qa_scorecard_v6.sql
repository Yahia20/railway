-- 034 — the v6 chat scorecard: its results table, its queue, its own switch,
-- and the flag that keeps an agent out of review.
--
-- WHAT THIS IS. v5.2 with every fix from the 2026-11-01 review of the trial
-- and the owner's decisions of 2026-11-03/04; approved for production by the
-- QA owner on 2026-11-05. Code in services/worker/app/qa/v6; workflow 13
-- claims a chat here, asks POST /qa/v6/evaluate, stores the answer.
--
-- WHY A NEW TABLE and not qa_grades. qa_grades holds 496 v4 grades on a
-- different rubric (nine items, red flags). The same reasoning as 031: no report
-- may average two rubrics together without saying so. v4 rows stay as they are,
-- and switching v6 off (`v6_mode`) leaves v4's switch (`mode`) untouched.
--
-- WHICH CHATS (owner, 2026-11-03): only chats whose deal ENDED, won or lost; a
-- running deal is not graded. The customer wrote and a human agent replied, and
-- the chat has been quiet for 24 hours, which is when follow-up is judged. A
-- graded chat comes back when its deal changes stage (a deal reopened and lost
-- again) or when the conversation went on after it was graded.
--
-- WHO IS LEFT OUT. agents.qa_excluded, set from the git-ignored roster by
-- scripts/seed_agents.py. Names never enter this public repository (rule 7).

BEGIN;

SET lock_timeout = '5s';

ALTER TABLE agents ADD COLUMN IF NOT EXISTS qa_excluded boolean NOT NULL DEFAULT false;
COMMENT ON COLUMN agents.qa_excluded IS
  'Chats whose first human agent is this account are not graded by the v6 scorecard (owner decision 2026-10-17). '
  'Set from local-reports/agent_roster.json by scripts/seed_agents.py.';

-- ---------------------------------------------------------------------------
-- Results, one row per chat, which is also the queue's state
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qa_grades_v6 (
  interaction_id        uuid PRIMARY KEY REFERENCES interactions (interaction_id) ON DELETE CASCADE,
  status                text NOT NULL
                        CHECK (status IN ('claimed', 'scored', 'not_gradeable', 'failed')),
  agent_id              uuid REFERENCES agents (agent_id),
  employee              text,
  -- single: one known typist, counts for that agent. shared: two or more agents
  -- wrote. uncertain: signed with someone else's name. Only `single` counts
  -- toward an agent; the others are shown, never averaged.
  attribution           text CHECK (attribution IN ('single', 'shared', 'uncertain')),
  deal_stage            text,
  outcome               text CHECK (outcome IN ('won', 'lost')),
  prompt_version        text,
  alerts_prompt_version text,
  engine_version        text,
  model                 text,
  votes                 smallint,
  -- NULL when nothing applied, and always NULL for a chat that is not a real
  -- ask (job seeker, supplier...): those get a `title` and no score.
  score                 smallint CHECK (score BETWEEN 0 AND 100),
  earned                numeric(8, 3),
  total                 numeric(8, 3),
  chat_type             text CHECK (chat_type IN ('sales', 'visa', 'after_sales', 'other')),
  title                 text,
  loss_reason           text,
  -- Checks a person must decide. The score leaves them out until then.
  reviews_open          smallint,
  -- The scorecard's whole result (checks with evidence lines, alerts) and the
  -- masked transcript it was computed from: enough to redraw the chat on a
  -- page and to replay a rule change without paying again. Customer words, so
  -- workflow 13 blanks both, and `answers`, with the 90-day purge.
  result                jsonb,
  transcript            jsonb,
  answers               jsonb,
  cost_usd              numeric(10, 6),
  reason                text,
  attempts              smallint NOT NULL DEFAULT 0,
  claim_token           uuid,
  claimed_at            timestamptz,
  observed_at           timestamp,           -- Riyadh wall time the grade was taken at ("pending" is relative to it)
  evaluated_at          timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT qa_grades_v6_scored_is_complete
    CHECK (status <> 'scored' OR (prompt_version IS NOT NULL AND engine_version IS NOT NULL AND result IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS qa_grades_v6_agent_idx ON qa_grades_v6 (agent_id, evaluated_at) WHERE status = 'scored';
CREATE INDEX IF NOT EXISTS qa_grades_v6_evaluated_idx ON qa_grades_v6 (evaluated_at) WHERE status = 'scored';

-- ---------------------------------------------------------------------------
-- Switches. v6 has its own: v4's `mode` keeps meaning v4.
-- ---------------------------------------------------------------------------
INSERT INTO qa_config (key, value) VALUES
  ('v6_mode',  'off'),          -- 'on' to grade
  ('v6_since', '2026-11-01'),   -- chats that started on or after this Riyadh date
  ('v6_batch', '4')             -- chats per 10-minute tick; ~60-90 s each keeps a tick inside its 9-minute limit
ON CONFLICT (key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- The queue
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_qa6_grade_due AS
SELECT i.interaction_id, i.started_at, d.stage_semantic AS deal_stage
FROM interactions i
JOIN deals d USING (deal_id)
LEFT JOIN qa_grades_v6 g USING (interaction_id)
-- The first human agent: not the integration account ('1'), not a bot, not a reaction.
CROSS JOIN LATERAL (
  SELECT m.sender_external_id AS ext
  FROM chat_messages m
  WHERE m.interaction_id = i.interaction_id
    AND m.sender = 'agent'
    AND m.sender_external_id IS NOT NULL AND m.sender_external_id <> '1'
    AND m.content_type IS DISTINCT FROM 'reaction'
    AND NOT EXISTS (SELECT 1 FROM agents b WHERE b.bitrix_user_id = m.sender_external_id AND b.is_bot)
  ORDER BY m.sent_at, m.seq
  LIMIT 1) fa
LEFT JOIN agents a ON a.bitrix_user_id = fa.ext
WHERE i.external_source = 'bitrix_chat_api'
  AND i.content_purged_at IS NULL
  AND d.stage_semantic IN ('S', 'F')
  AND i.customer_message_count >= 1
  AND i.started_at >= ((SELECT value FROM qa_config WHERE key = 'v6_since')::date::timestamp
                       AT TIME ZONE 'Asia/Riyadh')
  AND coalesce(i.ended_at, i.started_at) < now() - interval '24 hours'
  AND NOT coalesce(a.qa_excluded, false)
  AND (g.interaction_id IS NULL
       OR (g.status = 'claimed' AND g.claimed_at < now() - interval '30 minutes')
       OR (g.status = 'failed' AND g.attempts < 3 AND g.updated_at < now() - interval '1 hour')
       -- graded, then the deal moved (reopened and closed again) or the chat went on
       OR (g.status IN ('scored', 'not_gradeable')
           AND (g.deal_stage IS DISTINCT FROM d.stage_semantic
                OR coalesce(i.ended_at, i.started_at) > coalesce(g.evaluated_at, g.updated_at))));

COMMENT ON VIEW v_qa6_grade_due IS
  'Chats the v6 scorecard should grade now: deal won or lost, customer wrote, a human agent replied, quiet 24 h, '
  'agent not excluded, since qa_config.v6_since. Workflow 13 claims from here.';

-- ---------------------------------------------------------------------------
-- May v6 spend? Every money rule of v_qa_gate (030), on v6's own switch.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_qa6_gate AS
SELECT coalesce(c.value, 'off')          AS mode,
       s.spend_mtd_usd,
       s.monthly_cap_usd,
       st.balance_usd,
       st.checked_at,
       CASE
         WHEN coalesce(c.value, 'off') <> 'on'                         THEN false
         WHEN s.hard_stop AND s.over_cap                               THEN false
         WHEN st.provider IS NULL OR st.available IS DISTINCT FROM true THEN false
         WHEN st.checked_at < now() - interval '1 hour'                THEN false
         ELSE true
       END AS may_run,
       CASE
         WHEN coalesce(c.value, 'off') <> 'on'                         THEN 'qa_config.v6_mode is off'
         WHEN s.hard_stop AND s.over_cap
           THEN format('monthly cap reached: %s of %s USD spent',
                       round(s.spend_mtd_usd, 2), s.monthly_cap_usd)
         WHEN st.provider IS NULL                                      THEN 'no preflight has run yet - balance unknown'
         WHEN st.available IS DISTINCT FROM true
           THEN coalesce(st.reason, 'provider reports no available balance')
         WHEN st.checked_at < now() - interval '1 hour'                THEN 'balance check is older than an hour'
       END AS reason
FROM v_spend_mtd s
LEFT JOIN provider_status st ON st.provider = s.provider
LEFT JOIN qa_config c ON c.key = 'v6_mode'
WHERE s.provider = 'deepseek';

COMMIT;
