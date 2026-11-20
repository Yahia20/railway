-- 041 — a call belongs to the employee who SAID their name in it.
--
-- The owner's rule, 2026-11-20: someone answering from a colleague's phone
-- says "معاك سارة", and the call is Sara's, not the phone owner's. When the
-- agent never says a name, the call belongs to nobody and the portal says
-- "لم يتم ذكر الاسم في الكول".
--
-- The worker (prompt call-role-3, app/calls_audio/service.py name_check)
-- writes call_processing_jobs.metrics->'name_check' with "v": 3 and a status:
--   matched       agent_id = the one employee carrying that name, or the
--                 line's owner when several share it (method by_line)
--   not_said      reason name_not_said
--   unknown_name  reason name_unknown   (a name, but no employee's)
--   ambiguous     reason name_ambiguous (several share it, none is the line)
-- A matched person who is inactive, a bot or qa_excluded is account_excluded,
-- exactly as a phone mapped to them was.
--
-- Before a call is transcribed there is no name yet, so the phone decides as
-- in 039 (that row is provisional: a call is graded only after transcription).
-- Rows written by call-role-2 (no "v") keep 039's reading.
--
-- Same columns, same order as 039: app/portal/api.py and the worker read them
-- by name, and line_agent_id is still the phone's owner, which the worker uses
-- to settle a shared name.
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
  LEFT JOIN call_processing_jobs p USING (uniqueid)),
named AS (
  SELECT b.*, (b.nc ->> 'v') = '3' AS by_name, b.nc ->> 'status' AS name_status, na.agent_id AS name_agent_id,
         (na.agent_id IS NOT NULL AND na.is_active AND NOT na.is_bot AND NOT na.qa_excluded) AS name_agent_ok
  FROM base b
  LEFT JOIN agents na ON (b.nc ->> 'v') = '3' AND b.nc ->> 'status' = 'matched'
                     AND na.agent_id::text = b.nc ->> 'agent_id')
SELECT uniqueid, call_type, calldate_local, handled_extension, evidence,
       CASE WHEN by_name THEN
                 CASE name_status WHEN 'matched' THEN CASE WHEN name_agent_ok THEN 'name_said' ELSE 'account_excluded' END
                                  WHEN 'unknown_name' THEN 'name_unknown'
                                  WHEN 'ambiguous' THEN 'name_ambiguous'
                                  ELSE 'name_not_said' END
            WHEN line_agent_id IS NOT NULL AND nc ->> 'verdict' = 'no'
                 AND nc ->> 'owner_id' = line_agent_id::text THEN 'name_mismatch'
            ELSE line_reason END AS reason,
       CASE WHEN by_name THEN CASE WHEN name_status = 'matched' AND name_agent_ok THEN name_agent_id END
            WHEN nc ->> 'verdict' = 'no' AND nc ->> 'owner_id' = line_agent_id::text THEN NULL
            ELSE line_agent_id END AS agent_id,
       line_agent_id,
       nc ->> 'said' AS name_said
FROM named;

COMMIT;
