-- 040 — the transcription gate asks about the provider the worker really uses.
--
-- v_call_audio_gate (037) asked the `anthropic` row of v_pipeline_gate, because
-- the agent-voice question was meant to run on Haiku. The Anthropic key is gone
-- and the question runs on DeepSeek, so a closed Anthropic gate could stop
-- transcription that spends nothing there, and an open one let DeepSeek spend
-- past its own gate (Codex review, 2026-11-18).
--
-- `role_provider` is now one setting both sides read: the worker (calls_audio/
-- service.py) uses Haiku only when it says 'anthropic' AND the key is set; the
-- gate asks that provider's row. On DeepSeek the chat reserve of 038 applies
-- too, so calls never take the last slice of the month from chat grading.
-- Same columns as 037.
BEGIN;
SET LOCAL lock_timeout = '5s';

INSERT INTO call_pipeline_config (key, value) VALUES ('role_provider', 'deepseek')
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE VIEW v_call_audio_gate AS
SELECT coalesce(c.value, 'off') AS mode, s.spent_usd, s.cap_usd,
       (coalesce(c.value, 'off') = 'on' AND s.under_cap AND coalesce(g.may_run, false)
        AND NOT coalesce(rp.value = 'deepseek' AND m.spend_mtd_usd >= m.monthly_cap_usd - coalesce(r.value::numeric, 0), false)
       ) AS may_run,
       CASE WHEN coalesce(c.value, 'off') <> 'on' THEN 'call_pipeline_config.audio_mode is off'
            WHEN NOT s.under_cap THEN format('calls lane cap reached: %s of %s USD', s.spent_usd, s.cap_usd)
            WHEN NOT coalesce(g.may_run, false)
              THEN coalesce(rp.value, 'deepseek') || ': ' || coalesce(g.reason, 'no gate row')
            WHEN rp.value = 'deepseek' AND m.spend_mtd_usd >= m.monthly_cap_usd - coalesce(r.value::numeric, 0)
              THEN format('the last %s USD of the DeepSeek cap is kept for chat grading', r.value)
       END AS reason
FROM v_call_lane_spend s
LEFT JOIN call_pipeline_config c  ON c.key = 'audio_mode'
LEFT JOIN call_pipeline_config rp ON rp.key = 'role_provider'
LEFT JOIN call_pipeline_config r  ON r.key = 'chat_reserve_usd'
LEFT JOIN v_pipeline_gate g ON g.provider = coalesce(rp.value, 'deepseek')
LEFT JOIN v_spend_mtd m ON m.provider = coalesce(rp.value, 'deepseek');

COMMIT;
