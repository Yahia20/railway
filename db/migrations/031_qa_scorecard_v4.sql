-- 031 — the v4 chat scorecard (the owner's handoff of 2026-10-11): its own
-- results table, its queue, the switches for the loss-reason model, and a
-- second paid provider (Anthropic) that the spend views can tell apart.
--
-- WHAT THIS IS. Nine weighted items -> a 0-100 chat score, a loss reason, a red
-- flag ("qualified customer got no offer"). Code in services/worker/app/qa/v4;
-- workflow 10 claims a chat here, asks POST /qa/evaluate, stores the answer.
--
-- WHY A NEW TABLE and not qa_evaluations. That table holds the 297 v0.5 trial
-- grades on a different scale (15 items on 0-5, critical errors). 030 gave
-- that rubric a table of its own so no report would average two rubrics
-- together without saying so; the same reasoning gives this one its own. The
-- v0.5 rows stay readable at /dashboard/v05.
--
-- WHY v_spend_mtd IS REWRITTEN. Its `judge` CTE summed EVERY model_calls row
-- under the label 'deepseek' — true while DeepSeek was the only provider, and
-- wrong the moment a Sonnet call lands: Anthropic's dollars would count against
-- DeepSeek's cap and be invisible against their own. It now groups by
-- model_calls.provider. Column list and types are unchanged, because
-- v_pipeline_gate, v_qa_gate, app/budget.py and /spend read them by name.
-- v_spend_by_component had the same hard-coded label.

BEGIN;

SET lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- Results, one row per chat, which is also the queue's state
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qa_grades (
  interaction_id     uuid PRIMARY KEY REFERENCES interactions (interaction_id) ON DELETE CASCADE,
  status             text NOT NULL
                     CHECK (status IN ('claimed', 'scored', 'not_gradeable', 'failed')),
  agent_id           uuid REFERENCES agents (agent_id),
  prompt_version     text,
  model              text,
  runs               smallint,
  -- NULL when no item applied (possible: every item has a condition except
  -- `correct` and `professional`, so in practice it is always set).
  score              smallint CHECK (score BETWEEN 0 AND 100),
  -- {"answered": {"value": 0.0, "lines": [4]}, ...} — only the items that applied.
  items              jsonb,
  chat_type          text CHECK (chat_type IN ('sales', 'visa', 'after_sales', 'other')),
  qualified          boolean,
  red_flag           text CHECK (red_flag IN ('no_offer', 'review_attachment')),
  loss_reason        text,
  loss_reason_source text CHECK (loss_reason_source IN ('sonnet', 'deepseek')),
  followup_attempts  smallint,
  -- The masked lines that prove each verdict, the booking quotes, reply times.
  -- Customer words: blanked with the 90-day purge (workflow 10's Retention).
  detail             jsonb,
  -- The model answers the grade was computed from. Kept so a rule change can
  -- be replayed without paying for the reading again, and so two grades of
  -- the same chat can be compared answer by answer. Blanked with the purge.
  answers            jsonb,
  cost_usd           numeric(10, 6),
  reason             text,
  attempts           smallint NOT NULL DEFAULT 0,
  claim_token        uuid,
  claimed_at         timestamptz,
  evaluated_at       timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT qa_grades_scored_is_complete
    CHECK (status <> 'scored' OR (prompt_version IS NOT NULL AND items IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS qa_grades_agent_idx ON qa_grades (agent_id) WHERE status = 'scored';
CREATE INDEX IF NOT EXISTS qa_grades_flag_idx ON qa_grades (red_flag) WHERE red_flag IS NOT NULL;

-- ---------------------------------------------------------------------------
-- The queue. The filter is 030's, unchanged — human agent, both sides wrote
-- twice, 4-60 lines, quiet for 3 days — and the 3 days matter more now: the
-- follow-up item needs room for two attempts to have happened (handoff 10).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_qa_grade_due AS
SELECT i.interaction_id, i.started_at
FROM interactions i
JOIN agents a USING (agent_id)
LEFT JOIN qa_grades g USING (interaction_id)
WHERE i.external_source = 'bitrix_chat_api'
  AND NOT a.is_bot AND a.is_active
  AND i.customer_message_count >= 2
  AND i.agent_message_count >= 2
  AND i.message_count BETWEEN 4 AND 60
  AND i.started_at >= ((SELECT value FROM qa_config WHERE key = 'since')::date::timestamp
                       AT TIME ZONE 'Asia/Riyadh')
  AND i.ended_at < now() - interval '3 days'
  AND i.content_purged_at IS NULL
  AND (g.interaction_id IS NULL
       OR (g.status = 'claimed' AND g.claimed_at < now() - interval '30 minutes')
       OR (g.status = 'failed' AND g.attempts < 3 AND g.updated_at < now() - interval '1 hour'));

-- ---------------------------------------------------------------------------
-- Where the loss reason comes from. 'deepseek' until the owner switches it:
--   UPDATE qa_config SET value = 'sonnet' WHERE key = 'loss_reason_model';
-- and the worker has ANTHROPIC_API_KEY. The owner measured ~$13 per 1,000 chats
-- per vote; at ~2,200 chats a month that is ~$29 for 1 vote, ~$85 for 3.
-- ---------------------------------------------------------------------------
INSERT INTO qa_config (key, value) VALUES
  ('loss_reason_model', 'deepseek'),
  ('sonnet_votes', '1')
ON CONFLICT (key) DO NOTHING;

-- Anthropic has no balance endpoint, so its only brake is this cap: a hard
-- stop at 20 USD a month, the same as DeepSeek's. The worker reads
-- v_pipeline_gate before every Sonnet call and falls back to the DeepSeek vote
-- (recorded as loss_reason_source = 'deepseek') when the gate is closed.
INSERT INTO provider_budgets (provider, display_name, monthly_cap_usd, hard_stop,
                              require_positive_balance, enabled, notes)
VALUES ('anthropic', 'Anthropic (Claude Sonnet, QA loss reason)', 20.00, true, false, true,
        '031: call 4 of the QA scorecard. No balance API - the cap is the brake.')
ON CONFLICT (provider) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Spend per provider, not everything under 'deepseek'
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_spend_mtd AS
WITH bounds AS (
  SELECT (date_trunc('month', now() AT TIME ZONE 'Asia/Riyadh') AT TIME ZONE 'Asia/Riyadh') AS month_start
), judge AS (
  SELECT mc.provider,
         coalesce(sum(mc.cost_usd), 0::numeric) AS spend_usd,
         count(*)                               AS units
    FROM model_calls mc, bounds
   WHERE mc.created_at >= bounds.month_start
   GROUP BY mc.provider
)
SELECT b.provider,
       b.display_name,
       b.monthly_cap_usd,
       b.hard_stop,
       b.enabled,
       coalesce(s.spend_usd, 0::numeric) AS spend_mtd_usd,
       coalesce(s.units, 0::bigint)      AS units_mtd,
       CASE WHEN b.monthly_cap_usd IS NULL THEN NULL::numeric
            ELSE greatest(b.monthly_cap_usd - coalesce(s.spend_usd, 0::numeric), 0::numeric)
       END AS remaining_usd,
       CASE WHEN b.monthly_cap_usd IS NULL OR b.monthly_cap_usd = 0::numeric THEN NULL::numeric
            ELSE round(100::numeric * coalesce(s.spend_usd, 0::numeric) / b.monthly_cap_usd, 1)
       END AS pct_of_cap,
       b.monthly_cap_usd IS NOT NULL AND coalesce(s.spend_usd, 0::numeric) >= b.monthly_cap_usd AS over_cap
  FROM provider_budgets b
  LEFT JOIN judge s ON s.provider = b.provider;

CREATE OR REPLACE VIEW v_spend_by_component AS
WITH bounds AS (
  SELECT (date_trunc('month', now() AT TIME ZONE 'Asia/Riyadh') AT TIME ZONE 'Asia/Riyadh') AS month_start
)
SELECT mc.provider,
       mc.purpose                                  AS component,
       count(*)                                    AS calls,
       count(*) FILTER (WHERE NOT mc.succeeded)    AS failed,
       sum(mc.prompt_tokens)                       AS prompt_tokens,
       sum(mc.cached_tokens)                       AS cached_tokens,
       sum(mc.output_tokens)                       AS output_tokens,
       round(sum(mc.cost_usd), 4)                  AS spend_usd,
       count(*) FILTER (WHERE mc.priced_at_peak)   AS at_peak_rate,
       min(mc.created_at)                          AS first_at,
       max(mc.created_at)                          AS last_at
  FROM model_calls mc, bounds
 WHERE mc.created_at >= bounds.month_start
 GROUP BY mc.provider, mc.purpose;

COMMIT;
