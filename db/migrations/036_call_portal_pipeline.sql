-- 036 — calls for the agent portal: who answered, the audio pipeline, the grade.
--
-- WHO ANSWERED is the whole point of this file. call_log.extension is the FIRST
-- leg that rang for an inbound call (one queue extension holds 57% of all
-- calls), which is the queue-3009 failure of 023 in a new shape. The PBX's
-- journey endpoint (/api/call-log/journey?linkedid=) records an ANSWER event
-- with the extension that actually picked up. Workflow 14 fetches it, the
-- worker reduces it (app/calls_archive/attribution.py: complete? one answering
-- extension? supervision?) and n8n stores the reduction here.
--
-- THE PERSON IS RESOLVED LIVE, in v_call_owner, from call_agent_mappings —
-- never stored on the call. A mapping corrected tomorrow re-attributes every
-- past call at once, with no re-run and no stale copy. An unmapped, unapproved,
-- excluded or ambiguous call has agent_id NULL and is shown in team totals
-- only, never on a person's page.
--
-- Nothing here spends money. The audio and grading stages each have a mode in
-- call_pipeline_config that ships 'off'.
BEGIN;
SET LOCAL lock_timeout = '5s';

-- Extension -> employee, approved by the owner (2026-11-15). Several
-- extensions may belong to one person; one extension belongs to one person.
CREATE TABLE IF NOT EXISTS call_agent_mappings (
  extension   text PRIMARY KEY,
  agent_id    uuid NOT NULL REFERENCES agents (agent_id),
  approved    boolean NOT NULL DEFAULT false,
  approved_at timestamptz,
  provenance  text NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (NOT approved OR approved_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS call_pipeline_config (
  key   text PRIMARY KEY,
  value text NOT NULL
);
INSERT INTO call_pipeline_config (key, value) VALUES
  ('journey_mode',      'off'),  -- workflow 14: read journeys (free; PBX only)
  ('journey_batch',     '20'),
  ('audio_mode',        'off'),  -- workflow 15: split + transcribe (Railway CPU)
  ('audio_batch',       '2'),
  ('grade_mode',        'off'),  -- workflow 16: grade (DeepSeek + Haiku)
  ('grade_batch',       '4'),
  ('min_audio_seconds', '20'),
  ('max_attempts',      '3'),
  ('since',             '2026-11-01'),
  -- The owner's money rule for building this lane (2026-11-14): at most 10 USD
  -- in total. v_call_lane_spend adds model spend and audio compute; the audio
  -- and grade claims stop when it reaches this.
  ('lifetime_cap_usd',  '10'),
  -- Railway's published standard rates (USD per vCPU-hour / GB-hour), used to
  -- price the audio service's measured run time. Change with an UPDATE.
  ('audio_vcpu',        '8'),
  ('audio_ram_gb',      '4'),
  ('vcpu_hour_usd',     '0.02778'),
  ('gb_hour_usd',       '0.01389')
ON CONFLICT (key) DO NOTHING;

-- One row per inbound call whose journey was read. Only the reduction is kept
-- (direction/answer/hangup/supervision events), not the 90 ring-group RING and
-- TRANSFER lines every queue call carries.
CREATE TABLE IF NOT EXISTS call_journeys (
  uniqueid           text PRIMARY KEY REFERENCES call_log (uniqueid),
  status             text NOT NULL CHECK (status IN ('claimed', 'read', 'retry_wait', 'failed')),
  verdict            text,               -- 'answered' | why not (attribution.py)
  answer_extension   text,               -- set only when exactly one extension answered
  answer_extensions  text[],
  events             jsonb,
  attempts           smallint NOT NULL DEFAULT 0,
  claim_token        uuid,
  claim_until        timestamptz,
  next_attempt_at    timestamptz NOT NULL DEFAULT now(),
  last_error         text,
  read_at            timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CHECK ((claim_token IS NULL) = (claim_until IS NULL)),
  CHECK (status <> 'read' OR verdict IS NOT NULL),
  CHECK (answer_extension IS NULL OR verdict = 'answered')
);
CREATE INDEX IF NOT EXISTS call_journeys_due ON call_journeys (status, next_attempt_at);

-- Which extension handled the call, and the person behind it, resolved live.
--   OUT: the PBX's own origin extension (reliable for outbound).
--   IN:  the single extension in the journey's ANSWER events.
CREATE OR REPLACE VIEW v_call_owner AS
SELECT l.uniqueid,
       l.call_type,
       l.calldate_local,
       CASE WHEN l.call_type = 'OUT' THEN nullif(trim(l.extension), '')
            ELSE j.answer_extension END                              AS handled_extension,
       CASE WHEN l.call_type = 'OUT' THEN 'outbound_origin'
            WHEN j.uniqueid IS NULL OR j.status <> 'read' THEN 'journey_not_read'
            ELSE j.verdict END                                        AS evidence,
       CASE
         WHEN l.call_type = 'IN' AND (j.uniqueid IS NULL OR j.status <> 'read') THEN 'journey_not_read'
         WHEN l.call_type = 'IN' AND j.verdict <> 'answered'                   THEN j.verdict
         WHEN m.extension IS NULL                                              THEN 'extension_unmapped'
         WHEN NOT m.approved                                                   THEN 'mapping_not_approved'
         WHEN NOT a.is_active OR a.is_bot OR a.qa_excluded                     THEN 'account_excluded'
         ELSE 'attributed'
       END                                                            AS reason,
       CASE WHEN m.approved AND a.is_active AND NOT a.is_bot AND NOT a.qa_excluded
                 AND (l.call_type = 'OUT' OR j.verdict = 'answered')
            THEN a.agent_id END                                       AS agent_id
FROM call_log l
LEFT JOIN call_journeys j USING (uniqueid)
LEFT JOIN call_agent_mappings m
       ON m.extension = CASE WHEN l.call_type = 'OUT' THEN nullif(trim(l.extension), '')
                             ELSE j.answer_extension END
LEFT JOIN agents a ON a.agent_id = m.agent_id;

-- The audio + transcript + grade queue, one row per stored recording.
CREATE TABLE IF NOT EXISTS call_processing_jobs (
  uniqueid          text PRIMARY KEY REFERENCES call_log (uniqueid),
  status            text NOT NULL DEFAULT 'pending' CHECK (status IN
                    ('pending', 'claimed', 'transcribed', 'grading', 'graded',
                     'empty', 'too_short', 'review', 'retry_wait', 'failed')),
  audio_seconds     numeric(8, 2),
  speech_seconds    numeric(8, 2),
  compute_seconds   numeric(8, 2),     -- measured wall time of the audio service
  stereo_key        text,              -- bucket key: agent left, customer right
  agent_voice       text CHECK (agent_voice IN ('A', 'B')),
  transcript        jsonb,             -- masked, speaker-labelled segments
  metrics           jsonb,             -- timings computed from the segments (rule 3)
  model_versions    jsonb,
  attempts          smallint NOT NULL DEFAULT 0,
  claim_token       uuid,
  claim_until       timestamptz,
  next_attempt_at   timestamptz NOT NULL DEFAULT now(),
  last_error        text,
  content_purged_at timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CHECK ((claim_token IS NULL) = (claim_until IS NULL))
);
CREATE INDEX IF NOT EXISTS call_jobs_due ON call_processing_jobs (status, next_attempt_at);

CREATE TABLE IF NOT EXISTS call_grades (
  uniqueid        text PRIMARY KEY REFERENCES call_processing_jobs (uniqueid),
  score           numeric(5, 1) CHECK (score BETWEEN 0 AND 100),
  assessed_weight numeric(5, 1) NOT NULL CHECK (assessed_weight BETWEEN 0 AND 100),
  reviews_open    smallint NOT NULL DEFAULT 0,
  items           jsonb,
  critical_errors jsonb,
  answers         jsonb,             -- the raw readings: a rule change replays for free
  rubric_version  text NOT NULL,
  prompt_version  text NOT NULL,
  cost_usd        numeric(10, 6) NOT NULL DEFAULT 0,
  evaluated_at    timestamptz NOT NULL DEFAULT now()
);

-- Everything this lane has cost, against the owner's lifetime cap: model calls
-- (purposes call_*) at their stored price, plus audio compute at Railway rates.
CREATE OR REPLACE VIEW v_call_lane_spend AS
WITH cfg AS (
  SELECT max(value) FILTER (WHERE key = 'lifetime_cap_usd')::numeric AS cap,
         max(value) FILTER (WHERE key = 'audio_vcpu')::numeric        AS vcpu,
         max(value) FILTER (WHERE key = 'audio_ram_gb')::numeric      AS ram,
         max(value) FILTER (WHERE key = 'vcpu_hour_usd')::numeric     AS vcpu_rate,
         max(value) FILTER (WHERE key = 'gb_hour_usd')::numeric       AS ram_rate
  FROM call_pipeline_config
),
models AS (
  SELECT coalesce(sum(cost_usd), 0) AS usd FROM model_calls WHERE purpose LIKE 'call\_%'
),
audio AS (
  SELECT coalesce(sum(compute_seconds), 0) / 3600.0 AS hours FROM call_processing_jobs
)
SELECT cfg.cap                                                        AS cap_usd,
       round(models.usd, 4)                                           AS model_usd,
       round(audio.hours * (cfg.vcpu * cfg.vcpu_rate + cfg.ram * cfg.ram_rate), 4) AS audio_usd,
       round(models.usd + audio.hours * (cfg.vcpu * cfg.vcpu_rate + cfg.ram * cfg.ram_rate), 4) AS spent_usd,
       models.usd + audio.hours * (cfg.vcpu * cfg.vcpu_rate + cfg.ram * cfg.ram_rate) < cfg.cap AS under_cap
FROM cfg, models, audio;

GRANT SELECT ON call_agent_mappings, call_pipeline_config, call_journeys, v_call_owner,
                call_processing_jobs, call_grades, v_call_lane_spend
  TO portal_reader;
COMMIT;
