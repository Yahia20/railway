-- 026 — chat media archive: keep the files a conversation refers to.
--
-- WHAT WAS MISSING. The production chat API sends a file as text: a caption or
-- `[Attachment: name.ext]`, a newline, and a URL. We stored the text and so the
-- URL, never the file. Agent files sit on public Bitrix links that last; voice
-- notes on Bitrix REST download links that last; CUSTOMER images, PDFs and
-- videos on gupconnector links that answer 410 after about twenty minutes. So
-- every passport, transfer receipt and photo a customer sent is a dead link in
-- our store, and a pulled conversation reads as a list of URLs.
--
-- THE SHAPE (agreed with a read-only review by Codex/astra, 2026-10-01):
--
--   media_fetch_jobs   one per distinct source URL. The download queue, leased
--                      exactly like chat_eval_jobs (015): claim token AND
--                      deadline, every outcome fenced on the token.
--   chat_media         one per (message, attachment ordinal) → its job. This is
--                      the access boundary: a file is reached through a message,
--                      never by its hash.
--   media_objects      one per distinct content (sha256). Forty customers sent
--                      the same quote PDF = forty chat_media rows, one object.
--   chat_media_scan    which messages discovery has already examined, including
--                      the ones with no attachment, so the anti-join does not
--                      re-parse every text message every minute.
--   media_archive_config  mode and run lease for workflow 09.
--
-- NOTHING HERE EXPIRES ON A CLOCK. ~20 minutes is when customer links were
-- observed to die, not a rule anybody published, so it only sets priority. The
-- only "gone" signal is the source answering 404/410, and that moves a job to
-- recovery_pending (Bitrix still holds the file), never to a dead end.
--
-- RETENTION follows the conversation: a reference dies when its interaction is
-- older than the chat window (90 days from interactions.started_at, the same
-- rule as purge_raw_content), and bytes are deleted only when no live
-- reference remains. Workflow 09 enforces that itself, so media retention does
-- not depend on the chat purge succeeding — which it did not, every night from
-- 2026-09-16 until 025.
--
-- The worker never writes here (rule 11). It downloads and talks to the
-- bucket; workflow 09 writes every row.

BEGIN;

SET lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- media_objects — the bytes, once per content
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_objects (
  sha256           text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  bytes            bigint NOT NULL CHECK (bytes > 0),
  mime             text NOT NULL,           -- sniffed from the first bytes, not the source's claim
  storage_key      text NOT NULL,           -- sha256/<2>/<sha256>
  state            text NOT NULL DEFAULT 'present'
                   CHECK (state IN ('present', 'deleting', 'deleted')),
  first_stored_at  timestamptz NOT NULL DEFAULT now(),
  deleted_at       timestamptz,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CHECK ((state = 'deleted') = (deleted_at IS NOT NULL))
);
COMMENT ON TABLE media_objects IS
  'One row per distinct file content in the private bucket. Carries no customer, '
  'file name or access rule: those live on chat_media, because one object can be '
  'referenced from many conversations.';

-- ---------------------------------------------------------------------------
-- media_fetch_jobs — one per source URL
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_fetch_jobs (
  job_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  url_hash         text NOT NULL UNIQUE CHECK (url_hash ~ '^[0-9a-f]{64}$'),
  family           text NOT NULL
                   CHECK (family IN ('bitrix_short', 'bitrix_rest', 'gupconnector', 'gupshup')),
  -- The URL itself. For bitrix_rest it carries a live REST token, so it is
  -- nulled the moment the job reaches any terminal state, success or not.
  source_url       text,
  -- Earliest moment we saw this URL. Never moved later by a second delivery.
  first_seen_at    timestamptz NOT NULL,
  status           text NOT NULL DEFAULT 'pending',
  attempts         int  NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at  timestamptz NOT NULL DEFAULT now(),
  claimed_at       timestamptz,
  claim_until      timestamptz,
  claim_token      uuid,
  sha256           text REFERENCES media_objects(sha256),
  http_status      int,
  last_outcome     text,                    -- the worker's outcome word, verbatim
  last_error       text,                    -- never contains the URL
  -- A purged job's bucket receipt is deleted by a later call that can fail;
  -- NULL until the worker confirms it, so the next run asks again.
  receipt_deleted_at timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (status IN (
    'pending',           -- waiting for its turn
    'fetching',          -- claimed; a download is in flight
    'retry_wait',        -- transient failure (timeout, 429, 5xx, bucket); retried with backoff
    'stored',            -- bytes in the bucket, sha256 set. Terminal, the happy path.
    'recovery_pending',  -- the source said the file is gone (404/410). Bitrix
                         -- still holds it; recovery needs a credential that does
                         -- not exist yet, so this is also "blocked", and counted.
    'rejected',          -- policy: too large, not a file, a redirect off the
                         -- allow-list. Terminal, with the reason in last_outcome.
    'purged'             -- every reference to it expired. Terminal.
  )),
  CHECK ((claim_token IS NULL) = (claim_until IS NULL)),
  CHECK ((status = 'stored') = (sha256 IS NOT NULL)),
  CHECK (status NOT IN ('stored', 'rejected', 'purged') OR source_url IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_media_fetch_jobs_claimable
  ON media_fetch_jobs (next_attempt_at, first_seen_at)
  WHERE status IN ('pending', 'retry_wait');
CREATE INDEX IF NOT EXISTS idx_media_fetch_jobs_lease
  ON media_fetch_jobs (claim_until) WHERE status = 'fetching';
CREATE INDEX IF NOT EXISTS idx_media_fetch_jobs_status ON media_fetch_jobs (status);
CREATE INDEX IF NOT EXISTS idx_media_fetch_jobs_sha256
  ON media_fetch_jobs (sha256) WHERE sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_media_fetch_jobs_receipt_due
  ON media_fetch_jobs (updated_at) WHERE status = 'purged' AND receipt_deleted_at IS NULL;

DROP TRIGGER IF EXISTS t_media_fetch_jobs_updated ON media_fetch_jobs;
CREATE TRIGGER t_media_fetch_jobs_updated BEFORE UPDATE ON media_fetch_jobs
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS t_media_objects_updated ON media_objects;
CREATE TRIGGER t_media_objects_updated BEFORE UPDATE ON media_objects
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN media_fetch_jobs.first_seen_at IS
  'Earliest observation of this URL. Sets priority: gupconnector links were seen '
  'to die at ~20 minutes, so young customer links are fetched first. It is NOT an '
  'expiry: an overdue link is still tried, and only a 404/410 means gone.';

-- ---------------------------------------------------------------------------
-- chat_media — a file as it appears in a message
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_media (
  media_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id     bigint NOT NULL REFERENCES chat_messages(message_id) ON DELETE CASCADE,
  ordinal        smallint NOT NULL CHECK (ordinal >= 0),
  job_id         uuid NOT NULL REFERENCES media_fetch_jobs(job_id),
  declared_type  text,                      -- chat_messages.content_type as the API sent it
  file_name      text,                      -- from `[Attachment: …]`, when the API wrote one
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (message_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_chat_media_job ON chat_media (job_id);
COMMENT ON TABLE chat_media IS
  'One row per attachment occurrence. The only way to reach a file: the reader '
  'checks the message, its deal and its retention before handing out a link.';

-- ---------------------------------------------------------------------------
-- chat_media_scan — discovery state, so "nothing here" is remembered
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_media_scan (
  message_id   bigint PRIMARY KEY REFERENCES chat_messages(message_id) ON DELETE CASCADE,
  links_found  smallint NOT NULL CHECK (links_found >= 0),
  scanned_at   timestamptz NOT NULL DEFAULT now(),
  parser       text NOT NULL               -- version of the link rules that examined it
);

-- ---------------------------------------------------------------------------
-- media_archive_config — workflow 09's switches and run lease
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_archive_config (
  key         text PRIMARY KEY,
  value       text NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO media_archive_config (key, value) VALUES
  ('mode', 'off'),                  -- off | on. Deployed off; switched on by hand.
  ('retention_days', '90'),         -- must equal workflow 04's p_chat_days
  ('scan_batch', '300'),            -- messages examined per run
  ('fetch_batch', '3'),             -- downloads per run, one after another (75 s cap each)
  ('max_attempts', '8'),            -- transient failures before rejected
  ('run_lease_until', '1970-01-01T00:00:00Z'),
  ('run_lease_token', '')
ON CONFLICT (key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- v_media_health — what /report and a human look at
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_media_health AS
SELECT
  (SELECT count(*) FROM media_fetch_jobs WHERE status IN ('pending', 'retry_wait')) AS queued,
  (SELECT count(*) FROM media_fetch_jobs WHERE status = 'fetching')                 AS in_flight,
  (SELECT count(*) FROM media_fetch_jobs WHERE status = 'stored')                   AS stored,
  (SELECT count(*) FROM media_fetch_jobs WHERE status = 'recovery_pending')         AS recovery_pending,
  (SELECT count(*) FROM media_fetch_jobs WHERE status = 'rejected')                 AS rejected,
  (SELECT count(*) FROM media_fetch_jobs
    WHERE status IN ('pending', 'retry_wait') AND family = 'gupconnector'
      AND first_seen_at < now() - interval '15 minutes')                            AS customer_links_at_risk,
  (SELECT coalesce(sum(bytes), 0) FROM media_objects WHERE state = 'present')      AS bytes_stored,
  (SELECT max(scanned_at) FROM chat_media_scan)                                     AS last_scan_at,
  (SELECT value FROM media_archive_config WHERE key = 'mode')                       AS mode;

COMMIT;
