-- 033 — call recordings: do not download calls too short to hold a conversation.
--
-- Owner's request (2026-11-03): stop spending the PBX's night window on calls
-- that are useless for transcription and scoring.
--
-- WHY DURATION AND NOT `talk`. The PBX's `talk` counter is wrong for queue
-- calls: it reports the first ringing agent's leg (almost always the same
-- extension), not the conversation. Of the first 149 stored recordings, 119
-- had talk < 20 s, yet 51 of those held 20 s to 6 minutes of real audio. The
-- queue only starts recording once an agent answers, so an unanswered call
-- leaves a 44-byte header-only file. The call's total `duration` bounds the
-- audio from above: no call shorter than 28 s held 20 s of audio, and every
-- call shorter than 30 s was empty or under 20 s of audio but one.
--
-- Workflow 11 stops queueing calls under fetch_min_duration_seconds and
-- workflow 12 stops claiming them; the SQL of both reads this key with 30 as
-- the fallback. The calls stay in call_log, so any of them can be queued by
-- hand later.

BEGIN;

INSERT INTO call_archive_config (key, value) VALUES ('fetch_min_duration_seconds', '30')
ON CONFLICT (key) DO NOTHING;

-- Jobs already queued for calls under the threshold will never be claimed;
-- drop them so the health view's `queued` counts real work only. Only jobs
-- never attempted are touched.
DELETE FROM call_recording_jobs j
USING call_log l
WHERE l.uniqueid = j.uniqueid
  AND j.status = 'pending' AND j.attempts = 0
  AND coalesce(l.duration_seconds, 0) < 30;

COMMIT;
