-- 020 — money: what was spent, where, and a hard stop before spending more.
--
-- ═══════════════════════════════════════════════════════════════════════════
-- WHY THIS EXISTS, in one measurement.
--
-- DeepSeek's balance is  -0.10 USD,  is_available: false.  The judge cannot
-- run. Nothing in the pipeline knows that. Tonight's window would have claimed
-- 599 threads ten at a time, failed every one, incremented judge_attempts, and
-- dead-lettered the lot inside three nights — destroying the queue for a reason
-- that has nothing to do with the conversations in it. That is exactly what
-- already happened once for a different reason (the thinking bug), and the
-- lesson is the same both times: A PIPELINE THAT CANNOT TELL "BROKEN" FROM
-- "OUT OF MONEY" CONVERTS AN OUTAGE INTO PERMANENT DATA LOSS.
--
-- The rule this file enforces:
--
--   Stop BEFORE claiming, not after failing.
--
-- A job that is never claimed keeps its status, keeps judge_attempts, and
-- keeps its place in the queue. An outage then costs exactly nothing and loses
-- exactly nothing, however long it lasts, and the work resumes on its own when
-- the money comes back. Stopping after the failure would burn an attempt per
-- job per tick.
-- ═══════════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1 · provider_budgets — the policy. A table, not a constant, so raising a cap
--     is an UPDATE and not a deploy (same reasoning as alert_rules).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provider_budgets (
  provider         text PRIMARY KEY,
  display_name     text NOT NULL,

  -- The ceiling. NULL means "no cap of our own" — the provider's own balance
  -- is then the only limit, which is right for a pre-paid account.
  monthly_cap_usd  numeric(10,2),

  -- What to do at the ceiling. true = refuse to start work. This is the flag
  -- that makes "never spend more than the 30 free dollars" a property of the
  -- system rather than an intention.
  hard_stop        boolean NOT NULL DEFAULT true,

  -- Refuse to start work when the provider says it has no money, even if we
  -- are under our own cap. Separate from hard_stop because they are different
  -- failures: one is our budget, the other is their balance.
  require_positive_balance boolean NOT NULL DEFAULT true,

  enabled          boolean NOT NULL DEFAULT true,
  notes            text,
  updated_at       timestamptz NOT NULL DEFAULT now()
);

INSERT INTO provider_budgets
  (provider, display_name, monthly_cap_usd, hard_stop, require_positive_balance, notes)
VALUES
  ('deepseek', 'DeepSeek API', 20.00, true, true,
   'Judging, both passes. Pre-paid: the account balance is the real limit and '
   'the cap is a second belt. At 1,953 output tokens per conversation and '
   '~$0.67 per 1M output tokens, 20 USD is roughly 15,000 conversations.'),
  ('modal', 'Modal GPU (ASR)', 30.00, true, false,
   'HARD 30 USD: the free monthly credit and not a dollar more. '
   'require_positive_balance is false because Modal exposes no balance API - '
   'our own metered spend in asr_runs is the only signal, so the cap is the '
   'whole control.'),
  ('cohere', 'Cohere transcribe (fallback ASR)', 5.00, true, false,
   'Only used when ASR_BACKEND=cohere_api. Capped low on purpose: it is the '
   'fallback, and a fallback that quietly becomes the main path is how a bill '
   'arrives without a decision.')
ON CONFLICT (provider) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2 · provider_status — the live verdict, written by the worker's preflight.
--     One row per provider, overwritten each check. History lives in job_runs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provider_status (
  provider        text PRIMARY KEY REFERENCES provider_budgets(provider),
  checked_at      timestamptz NOT NULL DEFAULT now(),
  available       boolean NOT NULL,
  reason          text,
  balance_usd     numeric(12,4),
  spend_mtd_usd   numeric(12,4),
  raw             jsonb NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE provider_status IS
  'Whether each paid provider may be used right now, and why not. Written by '
  'the worker preflight; read by every workflow gate before it claims work.';

-- ---------------------------------------------------------------------------
-- 3 · v_spend_mtd — what has actually been spent this calendar month, per
--     provider, from the two tables that meter it.
--
--     model_calls is the only record of what judging costs (rule 12) and
--     asr_runs the only record of what transcription costs. Neither is an
--     estimate: both are written by the code that made the call.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_spend_mtd AS
WITH bounds AS (
  SELECT date_trunc('month', now() AT TIME ZONE 'Asia/Riyadh')
           AT TIME ZONE 'Asia/Riyadh' AS month_start
),
judge AS (
  SELECT 'deepseek'::text            AS provider,
         coalesce(sum(cost_usd), 0)  AS spend_usd,
         count(*)                    AS units
  FROM model_calls, bounds
  WHERE created_at >= bounds.month_start
),
asr AS (
  SELECT 'modal'::text                   AS provider,
         coalesce(sum(est_cost_usd), 0)  AS spend_usd,
         count(*)                        AS units
  FROM asr_runs, bounds
  WHERE started_at >= bounds.month_start
)
SELECT b.provider,
       b.display_name,
       b.monthly_cap_usd,
       b.hard_stop,
       b.enabled,
       coalesce(s.spend_usd, 0)                          AS spend_mtd_usd,
       coalesce(s.units, 0)                              AS units_mtd,
       CASE WHEN b.monthly_cap_usd IS NULL THEN NULL
            ELSE greatest(b.monthly_cap_usd - coalesce(s.spend_usd, 0), 0)
       END                                               AS remaining_usd,
       CASE WHEN b.monthly_cap_usd IS NULL OR b.monthly_cap_usd = 0 THEN NULL
            ELSE round(100 * coalesce(s.spend_usd, 0) / b.monthly_cap_usd, 1)
       END                                               AS pct_of_cap,
       (b.monthly_cap_usd IS NOT NULL
        AND coalesce(s.spend_usd, 0) >= b.monthly_cap_usd)  AS over_cap
FROM provider_budgets b
LEFT JOIN (SELECT * FROM judge UNION ALL SELECT * FROM asr) s
       ON s.provider = b.provider;

COMMENT ON VIEW v_spend_mtd IS
  'Spend this calendar month per provider against its cap, metered from '
  'model_calls and asr_runs. `over_cap` is what the hard stop reads.';

-- ---------------------------------------------------------------------------
-- 4 · v_pipeline_gate — one row per provider: may work start, and if not, why.
--
--     THE SINGLE PLACE A WORKFLOW ASKS. A gate that each workflow re-derives
--     for itself is a gate that acquires a fourth condition in only one of
--     them. Both 01d and 02 read this view and nothing else.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_pipeline_gate AS
SELECT s.provider,
       s.display_name,
       s.enabled,
       s.spend_mtd_usd,
       s.monthly_cap_usd,
       s.remaining_usd,
       st.balance_usd,
       st.checked_at,
       CASE
         WHEN NOT s.enabled THEN false
         WHEN s.hard_stop AND s.over_cap THEN false
         WHEN b.require_positive_balance
              AND st.available IS NOT DISTINCT FROM false THEN false
         -- No preflight has ever run: fail OPEN for providers we cannot probe
         -- (Modal has no balance API) and CLOSED for ones we can. An unchecked
         -- DeepSeek is exactly the state that emptied the queue.
         WHEN st.provider IS NULL AND b.require_positive_balance THEN false
         ELSE true
       END AS may_run,
       CASE
         WHEN NOT s.enabled
           THEN 'disabled in provider_budgets'
         WHEN s.hard_stop AND s.over_cap
           THEN format('monthly cap reached: %s of %s USD spent',
                       round(s.spend_mtd_usd, 2), s.monthly_cap_usd)
         WHEN b.require_positive_balance
              AND st.available IS NOT DISTINCT FROM false
           THEN coalesce(st.reason, 'provider reports no available balance')
         WHEN st.provider IS NULL AND b.require_positive_balance
           THEN 'no preflight has run yet - balance unknown'
         ELSE NULL
       END AS reason
FROM v_spend_mtd s
JOIN provider_budgets b ON b.provider = s.provider
LEFT JOIN provider_status st ON st.provider = s.provider;

COMMENT ON VIEW v_pipeline_gate IS
  'May this provider be used right now, and if not, why. The only gate a '
  'workflow should read. Fails CLOSED for a provider whose balance we can '
  'check and have not.';

-- ---------------------------------------------------------------------------
-- 5 · v_spend_by_component — the report page. Where the money went.
--
--     Deliberately NOT just a total: a number with no breakdown cannot be
--     acted on. Each row is a thing that can be turned off independently.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_spend_by_component AS
WITH bounds AS (
  SELECT date_trunc('month', now() AT TIME ZONE 'Asia/Riyadh')
           AT TIME ZONE 'Asia/Riyadh' AS month_start
)
SELECT 'deepseek'                          AS provider,
       mc.purpose                          AS component,
       count(*)                            AS calls,
       count(*) FILTER (WHERE NOT mc.succeeded) AS failed,
       sum(mc.prompt_tokens)               AS prompt_tokens,
       sum(mc.cached_tokens)               AS cached_tokens,
       sum(mc.output_tokens)               AS output_tokens,
       round(sum(mc.cost_usd), 4)          AS spend_usd,
       count(*) FILTER (WHERE mc.priced_at_peak) AS at_peak_rate,
       min(mc.created_at)                  AS first_at,
       max(mc.created_at)                  AS last_at
FROM model_calls mc, bounds
WHERE mc.created_at >= bounds.month_start
GROUP BY mc.purpose

UNION ALL

SELECT 'modal',
       'asr_batch',
       count(*),
       count(*) FILTER (WHERE r.status <> 'succeeded'),
       NULL, NULL, NULL,
       round(coalesce(sum(r.est_cost_usd), 0), 4),
       0,
       min(r.started_at),
       max(r.started_at)
FROM asr_runs r, bounds
WHERE r.started_at >= bounds.month_start;

COMMENT ON VIEW v_spend_by_component IS
  'This month''s spend broken down by the thing that spent it. Each row is '
  'independently switchable, which is what makes the number actionable.';

COMMIT;
