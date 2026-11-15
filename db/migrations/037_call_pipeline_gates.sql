-- 037 — what the audio (workflow 15) and grading (workflow 16) stages may take,
-- and whether they may spend. Views only; nothing is written.
--
-- THE GATES ASK BEFORE CLAIMING (the rule since 020): a closed gate claims
-- nothing, so an outage or a reached cap costs no attempt and loses no place.
--   audio : call_pipeline_config.audio_mode, the lane's lifetime cap (v_call_lane_spend,
--           Railway compute + model calls), and the anthropic row of v_pipeline_gate
--           (one Haiku question per call).
--   grade : call_pipeline_config.grade_mode, the lifetime cap, and every DeepSeek money
--           rule of v_pipeline_gate EXCEPT provider_budgets.enabled, which keeps the
--           retired v7 judge (01d) off and must not stop this — the same choice
--           v_qa6_gate (034) made for the chat scorecard.
BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE VIEW v_call_audio_due AS
SELECT r.uniqueid, l.calldate_local, r.storage_key
FROM call_recording_jobs r
JOIN call_log l USING (uniqueid)
LEFT JOIN call_processing_jobs j USING (uniqueid)
CROSS JOIN LATERAL (
  SELECT max(value) FILTER (WHERE key = 'since')::date           AS since,
         max(value) FILTER (WHERE key = 'min_audio_seconds')::int AS min_s,
         max(value) FILTER (WHERE key = 'max_attempts')::int      AS max_attempts
  FROM call_pipeline_config) c
WHERE r.status = 'stored'
  AND coalesce(r.bytes, 0) > 44 + 16000 * c.min_s        -- a header-only WAV is not a call (8 kHz, 16-bit)
  AND l.calldate_local >= c.since
  AND (j.uniqueid IS NULL
       OR (j.status = 'retry_wait' AND j.next_attempt_at <= now() AND j.attempts < c.max_attempts)
       OR (j.status = 'claimed' AND j.claim_until < now() AND j.attempts < c.max_attempts));

CREATE OR REPLACE VIEW v_call_grade_due AS
SELECT j.uniqueid, l.calldate_local
FROM call_processing_jobs j
JOIN call_log l USING (uniqueid)
CROSS JOIN LATERAL (SELECT max(value)::int AS max_attempts FROM call_pipeline_config WHERE key = 'max_attempts') c
WHERE (j.status = 'transcribed' AND j.next_attempt_at <= now())
   OR (j.status = 'grading' AND j.claim_until < now() AND j.attempts < c.max_attempts);

CREATE OR REPLACE VIEW v_call_audio_gate AS
SELECT coalesce(c.value, 'off') AS mode, s.spent_usd, s.cap_usd,
       (coalesce(c.value, 'off') = 'on' AND s.under_cap AND coalesce(g.may_run, false)) AS may_run,
       CASE WHEN coalesce(c.value, 'off') <> 'on' THEN 'call_pipeline_config.audio_mode is off'
            WHEN NOT s.under_cap THEN format('calls lane cap reached: %s of %s USD', s.spent_usd, s.cap_usd)
            WHEN NOT coalesce(g.may_run, false) THEN 'anthropic: ' || coalesce(g.reason, 'no gate row')
       END AS reason
FROM v_call_lane_spend s
LEFT JOIN call_pipeline_config c ON c.key = 'audio_mode'
LEFT JOIN v_pipeline_gate g ON g.provider = 'anthropic';

CREATE OR REPLACE VIEW v_call_grade_gate AS
SELECT coalesce(c.value, 'off') AS mode, l.spent_usd, l.cap_usd, st.balance_usd, st.checked_at,
       CASE
         WHEN coalesce(c.value, 'off') <> 'on'                         THEN false
         WHEN NOT l.under_cap                                          THEN false
         WHEN s.hard_stop AND s.over_cap                               THEN false
         WHEN st.provider IS NULL OR st.available IS DISTINCT FROM true THEN false
         WHEN st.checked_at < now() - interval '1 hour'                THEN false
         ELSE true
       END AS may_run,
       CASE
         WHEN coalesce(c.value, 'off') <> 'on' THEN 'call_pipeline_config.grade_mode is off'
         WHEN NOT l.under_cap THEN format('calls lane cap reached: %s of %s USD', l.spent_usd, l.cap_usd)
         WHEN s.hard_stop AND s.over_cap
           THEN format('monthly cap reached: %s of %s USD spent', round(s.spend_mtd_usd, 2), s.monthly_cap_usd)
         WHEN st.provider IS NULL THEN 'no preflight has run yet - balance unknown'
         WHEN st.available IS DISTINCT FROM true THEN coalesce(st.reason, 'provider reports no available balance')
         WHEN st.checked_at < now() - interval '1 hour' THEN 'balance check is older than an hour'
       END AS reason
FROM v_spend_mtd s
CROSS JOIN v_call_lane_spend l
LEFT JOIN provider_status st ON st.provider = s.provider
LEFT JOIN call_pipeline_config c ON c.key = 'grade_mode'
WHERE s.provider = 'deepseek';

GRANT SELECT ON v_call_audio_due, v_call_grade_due, v_call_audio_gate, v_call_grade_gate TO portal_reader;
COMMIT;
