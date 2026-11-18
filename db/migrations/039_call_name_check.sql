-- 039 — the name the agent SAID is checked against the line that answered.
--
-- The PBX knows which PHONE answered, not which person: someone answering from
-- a colleague's desk is credited to the colleague. The agent-voice question
-- (worker, prompt call-role-2) now also copies the name the agent introduced
-- themselves with and says whether it is the line's owner. Its answer lands in
-- call_processing_jobs.metrics->'name_check' through workflow 15's existing
-- store, so no workflow changes.
--
-- v_call_owner: same columns, same order, plus two at the end.
--   line_agent_id  who the PBX + approved mapping say (the old agent_id)
--   name_said      the name the agent said, when one was said
-- agent_id is NULL, reason 'name_mismatch', when the agent clearly said
-- another person's name. A missing or unclear name ('unknown') never takes a
-- call away from anyone.
--
-- v_call_audio_due: an inbound call waits until its journey has been read (or
-- given up on), so the owner is known when the name is asked about.
BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE VIEW v_call_owner AS
WITH base AS (
  SELECT l.uniqueid, l.call_type, l.calldate_local,
         CASE WHEN l.call_type = 'OUT' THEN nullif(trim(l.extension), '') ELSE j.answer_extension END AS handled_extension,
         CASE WHEN l.call_type = 'OUT' THEN 'outbound_origin'
              WHEN j.uniqueid IS NULL OR j.status <> 'read' THEN 'journey_not_read'
              ELSE j.verdict END AS evidence,
         CASE WHEN l.call_type = 'IN' AND (j.uniqueid IS NULL OR j.status <> 'read') THEN 'journey_not_read'
              WHEN l.call_type = 'IN' AND j.verdict <> 'answered' THEN j.verdict
              WHEN m.extension IS NULL THEN 'extension_unmapped'
              WHEN NOT m.approved THEN 'mapping_not_approved'
              WHEN NOT a.is_active OR a.is_bot OR a.qa_excluded THEN 'account_excluded'
              ELSE 'attributed' END AS line_reason,
         CASE WHEN m.approved AND a.is_active AND NOT a.is_bot AND NOT a.qa_excluded
                   AND (l.call_type = 'OUT' OR j.verdict = 'answered') THEN a.agent_id END AS line_agent_id,
         p.metrics -> 'name_check' AS nc
  FROM call_log l
  LEFT JOIN call_journeys j USING (uniqueid)
  LEFT JOIN call_agent_mappings m
         ON m.extension = CASE WHEN l.call_type = 'OUT' THEN nullif(trim(l.extension), '') ELSE j.answer_extension END
  LEFT JOIN agents a ON a.agent_id = m.agent_id
  LEFT JOIN call_processing_jobs p USING (uniqueid))
SELECT uniqueid, call_type, calldate_local, handled_extension, evidence,
       CASE WHEN line_agent_id IS NOT NULL AND nc ->> 'verdict' = 'no' THEN 'name_mismatch'
            ELSE line_reason END AS reason,
       CASE WHEN nc ->> 'verdict' = 'no' THEN NULL ELSE line_agent_id END AS agent_id,
       line_agent_id,
       nc ->> 'said' AS name_said
FROM base;

CREATE OR REPLACE VIEW v_call_audio_due AS
SELECT r.uniqueid, l.calldate_local, r.storage_key
FROM call_recording_jobs r
JOIN call_log l USING (uniqueid)
LEFT JOIN call_processing_jobs j USING (uniqueid)
LEFT JOIN call_journeys cj USING (uniqueid)
CROSS JOIN LATERAL (
  SELECT max(value) FILTER (WHERE key = 'since')::date           AS since,
         max(value) FILTER (WHERE key = 'min_audio_seconds')::int AS min_s,
         max(value) FILTER (WHERE key = 'max_attempts')::int      AS max_attempts
  FROM call_pipeline_config) c
WHERE r.status = 'stored'
  AND coalesce(r.bytes, 0) > 44 + 16000 * c.min_s        -- a header-only WAV is not a call (8 kHz, 16-bit)
  AND l.calldate_local >= c.since
  AND (l.call_type <> 'IN' OR cj.status IN ('read', 'failed'))
  AND (j.uniqueid IS NULL
       OR (j.status = 'retry_wait' AND j.next_attempt_at <= now() AND j.attempts < c.max_attempts)
       OR (j.status = 'claimed' AND j.claim_until < now() AND j.attempts < c.max_attempts));

GRANT SELECT ON v_call_owner, v_call_audio_due TO portal_reader;
COMMIT;
