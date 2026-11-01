-- 032 — call recordings archive: the phone calls the PBX recorded, kept in a
-- private bucket of their own, ready for transcription and scoring later.
--
-- WHERE THE CALLS COME FROM. The call-system vendor (Teleswift, an Asterisk
-- PBX at tgate.teleswift.net:8443) exposes two endpoints behind an API key and
-- an IP whitelist (n8n's three static egress IPs, enabled 2026-11-01):
--
--   GET /api/call-log                      newest first, 100 per page, cursor
--                                          pages backwards in time; no date
--                                          filter is honoured (from/to ignored)
--   GET /api/call-log/recording/{uniqueid} the WAV, ~1 MB per minute of audio
--
-- THE SHAPE (same pattern as 026 for the chat media archive):
--
--   call_log             one row per customer call (IN or OUT). INTERNAL calls
--                        between extensions are not kept at all — decided by
--                        the owner on 2026-11-01. `raw` keeps the API's row
--                        verbatim so a field the vendor adds later is not lost.
--   call_recording_jobs  one per call that has a recording file. The download
--                        queue, leased exactly like media_fetch_jobs: claim
--                        token AND deadline, every outcome fenced on the token.
--   call_archive_config  mode and pacing for workflows 11 and 12.
--
-- WHO DOES WHAT. The PBX admits whitelisted IPs only, and only the n8n
-- service has static egress IPs. So n8n talks to the PBX: workflow 11 indexes
-- the call log; workflow 12 claims one job at a time, downloads the WAV, and
-- POSTs the bytes to the worker (/calls/recording/store), which checks they
-- are audio and puts them in the `call-recordings` bucket. n8n records every
-- answer; the worker never touches these tables (CLAUDE.md rule 11).
--
-- THE PBX API KEY is not in this file (the repo is public). It lives in
-- call_archive_config under `pbx_api_key`, inserted at deploy time, the same
-- way the offer follow-up keeps its classifier key.
--
-- TIME. The PBX reports `calldate` in Riyadh local time with no zone marker
-- (verified against the recording file names, which carry the same clock).
-- Saudi Arabia has no daylight saving, so `calldate_utc` is a fixed 3-hour
-- shift, written by workflow 11 from PBX_TZ_OFFSET_HOURS = 3.
--
-- PACING. One download per run, one run every two minutes, inside a night
-- window (02:00–06:00 Riyadh) after the shift has ended — the owner's choice
-- so the vendor's server is never under load from us during working hours.

BEGIN;
SET lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS call_log (
  uniqueid            text PRIMARY KEY,
  linkedid            text,
  calldate_local      timestamp NOT NULL,        -- as the PBX sends it (Asia/Riyadh wall clock)
  calldate_utc        timestamptz NOT NULL,      -- calldate_local shifted by the PBX offset
  call_type           text NOT NULL CHECK (call_type IN ('IN', 'OUT')),
  route               text,
  app                 text,
  src                 text,
  dst                 text,
  phone_number        text,                      -- the customer's number as the PBX saw it
  customer_name       text,
  extension           text,
  agent_name          text,
  duration_seconds    int CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
  talk_seconds        int CHECK (talk_seconds IS NULL OR talk_seconds >= 0),
  disposition         text,
  status              text,
  canonical_status    text,
  ended_by            text,
  ended_by_agent      text,
  call_journey_count  int,
  is_supervision      boolean,
  recording_file      text,                      -- NULL when the PBX kept no recording
  recording_path      text,
  raw                 jsonb NOT NULL,
  first_seen_at       timestamptz NOT NULL DEFAULT now(),
  last_seen_at        timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_call_log_calldate   ON call_log (calldate_local DESC);
CREATE INDEX IF NOT EXISTS idx_call_log_phone      ON call_log (phone_number);
CREATE INDEX IF NOT EXISTS idx_call_log_extension  ON call_log (extension, calldate_local DESC);

COMMENT ON TABLE call_log IS
  'Customer phone calls (IN/OUT) from the Teleswift PBX call log. Internal '
  'extension-to-extension calls are deliberately not stored. Written by '
  'workflow 11 only.';

CREATE TABLE IF NOT EXISTS call_recording_jobs (
  uniqueid         text PRIMARY KEY REFERENCES call_log(uniqueid) ON DELETE CASCADE,
  status           text NOT NULL DEFAULT 'pending',
  attempts         int  NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at  timestamptz NOT NULL DEFAULT now(),
  claimed_at       timestamptz,
  claim_until      timestamptz,
  claim_token      uuid,
  sha256           text CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
  bytes            bigint CHECK (bytes IS NULL OR bytes > 0),
  mime             text,
  storage_key      text,                     -- calls/<yyyy>/<mm>/<dd>/<uniqueid>.wav
  stored_at        timestamptz,
  http_status      int,
  last_outcome     text,                     -- the worker's outcome word, verbatim
  last_error       text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (status IN (
    'pending',      -- waiting for its turn in the night window
    'fetching',     -- claimed; a download is in flight
    'retry_wait',   -- transient failure (timeout, 5xx, bucket); retried with backoff
    'stored',       -- bytes in the bucket. Terminal, the happy path.
    'missing',      -- the PBX answered 404: it lists a file it does not have. Terminal
                    --   unless somebody resets it by hand.
    'rejected',     -- policy: not audio, or over the size cap. Terminal.
    'failed'        -- max_attempts transient failures in a row. Terminal until reset.
  )),
  CHECK ((claim_token IS NULL) = (claim_until IS NULL)),
  CHECK ((status = 'stored') = (storage_key IS NOT NULL AND sha256 IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_call_recording_jobs_claimable
  ON call_recording_jobs (next_attempt_at) WHERE status IN ('pending', 'retry_wait');
CREATE INDEX IF NOT EXISTS idx_call_recording_jobs_lease
  ON call_recording_jobs (claim_until) WHERE status = 'fetching';
CREATE INDEX IF NOT EXISTS idx_call_recording_jobs_status ON call_recording_jobs (status);

DROP TRIGGER IF EXISTS t_call_log_updated ON call_log;
CREATE TRIGGER t_call_log_updated BEFORE UPDATE ON call_log
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS t_call_recording_jobs_updated ON call_recording_jobs;
CREATE TRIGGER t_call_recording_jobs_updated BEFORE UPDATE ON call_recording_jobs
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS call_archive_config (
  key         text PRIMARY KEY,
  value       text NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO call_archive_config (key, value) VALUES
  ('mode', 'off'),                 -- off | on. Deployed off; switched on after the first dry run.
  ('pbx_base_url', 'https://tgate.teleswift.net:8443'),
  ('index_window_minutes', '120'), -- workflow 11: each 15-minute index looks back this far
  ('index_max_pages', '5'),        -- workflow 11: pages of 100 per run, at most
  ('fetch_window_start', '2'),     -- workflow 12: first hour (Riyadh) downloads may run
  ('fetch_window_end', '6'),       -- workflow 12: downloads stop at this hour (exclusive)
  ('max_attempts', '5'),           -- transient failures before `failed`
  ('asr_min_talk_seconds', '20')   -- next stage: calls shorter than this are not transcribed
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE VIEW v_call_archive_health AS
SELECT
  (SELECT count(*) FROM call_log)                                                    AS calls_indexed,
  (SELECT max(calldate_local) FROM call_log)                                         AS newest_call,
  (SELECT max(last_seen_at) FROM call_log)                                           AS last_index_at,
  (SELECT count(*) FROM call_recording_jobs WHERE status IN ('pending', 'retry_wait')) AS queued,
  (SELECT count(*) FROM call_recording_jobs WHERE status = 'fetching')               AS in_flight,
  (SELECT count(*) FROM call_recording_jobs WHERE status = 'stored')                 AS stored,
  (SELECT count(*) FROM call_recording_jobs WHERE status = 'missing')                AS missing,
  (SELECT count(*) FROM call_recording_jobs WHERE status IN ('rejected', 'failed'))  AS rejected_or_failed,
  (SELECT coalesce(sum(bytes), 0) FROM call_recording_jobs WHERE status = 'stored')  AS bytes_stored,
  (SELECT max(stored_at) FROM call_recording_jobs)                                   AS last_stored_at,
  (SELECT value FROM call_archive_config WHERE key = 'mode')                         AS mode;

COMMIT;
