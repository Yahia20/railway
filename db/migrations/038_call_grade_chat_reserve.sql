-- 038 — call grading must never starve chat grading.
--
-- Both spend the one DeepSeek monthly cap (provider_budgets, hard stop). The
-- first real call grade cost 0.0105 USD, so ~1,100 calls a month could take the
-- whole cap and close v_qa6_gate for the v6 chat scorecard. The call gate now
-- stops while the month's DeepSeek spend is within `chat_reserve_usd` of the
-- cap; the chats keep that last slice to themselves. Same columns as 037.
BEGIN;
SET LOCAL lock_timeout = '5s';

INSERT INTO call_pipeline_config (key, value) VALUES ('chat_reserve_usd', '2')
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE VIEW v_call_grade_gate AS
SELECT coalesce(c.value, 'off') AS mode, l.spent_usd, l.cap_usd, st.balance_usd, st.checked_at,
       CASE
         WHEN coalesce(c.value, 'off') <> 'on'                         THEN false
         WHEN NOT l.under_cap                                          THEN false
         WHEN s.hard_stop AND s.over_cap                               THEN false
         WHEN s.spend_mtd_usd >= s.monthly_cap_usd - coalesce(r.value::numeric, 0) THEN false
         WHEN st.provider IS NULL OR st.available IS DISTINCT FROM true THEN false
         WHEN st.checked_at < now() - interval '1 hour'                THEN false
         ELSE true
       END AS may_run,
       CASE
         WHEN coalesce(c.value, 'off') <> 'on' THEN 'call_pipeline_config.grade_mode is off'
         WHEN NOT l.under_cap THEN format('calls lane cap reached: %s of %s USD', l.spent_usd, l.cap_usd)
         WHEN s.hard_stop AND s.over_cap
           THEN format('monthly cap reached: %s of %s USD spent', round(s.spend_mtd_usd, 2), s.monthly_cap_usd)
         WHEN s.spend_mtd_usd >= s.monthly_cap_usd - coalesce(r.value::numeric, 0)
           THEN format('the last %s USD of the DeepSeek cap is kept for chat grading (%s of %s spent)',
                       r.value, round(s.spend_mtd_usd, 2), s.monthly_cap_usd)
         WHEN st.provider IS NULL THEN 'no preflight has run yet - balance unknown'
         WHEN st.available IS DISTINCT FROM true THEN coalesce(st.reason, 'provider reports no available balance')
         WHEN st.checked_at < now() - interval '1 hour' THEN 'balance check is older than an hour'
       END AS reason
FROM v_spend_mtd s
CROSS JOIN v_call_lane_spend l
LEFT JOIN provider_status st ON st.provider = s.provider
LEFT JOIN call_pipeline_config c ON c.key = 'grade_mode'
LEFT JOIN call_pipeline_config r ON r.key = 'chat_reserve_usd'
WHERE s.provider = 'deepseek';

COMMIT;
