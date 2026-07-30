-- 003 — the conversations themselves: raw landing, interactions, chat messages,
-- call transcripts.

-- ---------------------------------------------------------------------------
-- raw_events — land the payload, change nothing.
-- If a load goes wrong you replay from here instead of re-reading Bitrix or
-- re-running ASR. payload_hash makes redelivery a no-op.
-- ---------------------------------------------------------------------------
CREATE TABLE raw_events (
  raw_id       bigserial PRIMARY KEY,
  source       text NOT NULL,              -- 'bitrix_webhook' | 'bitrix_rest' | 'drive_calls'
  external_ref text,                       -- dialog id, drive file id, ...
  payload      jsonb NOT NULL,
  payload_hash text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  process_error text,
  UNIQUE (source, payload_hash)
);
CREATE INDEX ON raw_events (processed_at) WHERE processed_at IS NULL;
CREATE INDEX ON raw_events (source, external_ref);

-- ---------------------------------------------------------------------------
-- interactions — one row per conversation (a chat thread, or a call).
-- customer_id is nullable on purpose: ingest must never block on identity
-- resolution. The RESOLVE step backfills it.
-- ---------------------------------------------------------------------------
CREATE TABLE interactions (
  interaction_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid REFERENCES customers(customer_id),
  agent_id         uuid REFERENCES agents(agent_id),
  deal_id          uuid,                   -- FK added in 005 once deals exists
  channel          channel NOT NULL,
  direction        direction,
  handled_by       speaker_role NOT NULL DEFAULT 'unknown',
  is_bot_handled   boolean NOT NULL DEFAULT false,

  -- Natural keys from the source systems, kept so re-ingest is idempotent.
  external_id      text NOT NULL,          -- 'chat15556' | drive file id | asterisk uniqueid
  external_source  text NOT NULL,          -- 'bitrix' | 'asterisk_drive'
  customer_phone_raw   text,
  customer_phone_e164  text,

  started_at       timestamptz NOT NULL,
  ended_at         timestamptz,
  duration_seconds int,

  -- Denormalised counters, maintained by the ingest job, never by an LLM.
  message_count            int NOT NULL DEFAULT 0,
  customer_message_count   int NOT NULL DEFAULT 0,
  agent_message_count      int NOT NULL DEFAULT 0,
  first_response_seconds   int,            -- agent's first reply latency
  avg_response_seconds     int,
  is_after_hours           boolean,

  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (external_source, external_id),
  CONSTRAINT phone_is_e164 CHECK (customer_phone_e164 IS NULL
                                  OR customer_phone_e164 ~ '^\+[1-9][0-9]{6,14}$')
);
CREATE INDEX ON interactions (customer_id, started_at DESC);
CREATE INDEX ON interactions (agent_id, started_at DESC);
CREATE INDEX ON interactions (customer_phone_e164);
CREATE INDEX ON interactions (started_at DESC);

CREATE TRIGGER t_interactions_updated BEFORE UPDATE ON interactions
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- chat_messages — one row per message.
-- The Bitrix webhook resends the ENTIRE conversation_history on every call, so
-- without the uniqueness key below you insert the same message once per webhook.
-- body_hash is generated, not supplied, so the guarantee cannot be bypassed by
-- a careless caller.
-- ---------------------------------------------------------------------------
CREATE TABLE chat_messages (
  message_id     bigserial PRIMARY KEY,
  interaction_id uuid NOT NULL REFERENCES interactions(interaction_id) ON DELETE CASCADE,
  seq            int NOT NULL,             -- position in the thread, 1-based
  sender         speaker_role NOT NULL,
  body           text NOT NULL,
  body_hash      text GENERATED ALWAYS AS (text_hash(body)) STORED,
  sent_at        timestamptz NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (interaction_id, sender, sent_at, body_hash)
);
CREATE INDEX ON chat_messages (interaction_id, seq);

-- ---------------------------------------------------------------------------
-- transcripts — one row per call, holding the ASR output.
-- asr_confidence is the column that lets you answer "does the model score calls
-- worse than chats, or is the transcript just bad?" — the first question you
-- will ask when the numbers look odd.
-- ---------------------------------------------------------------------------
CREATE TABLE transcripts (
  transcript_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  interaction_id    uuid NOT NULL UNIQUE REFERENCES interactions(interaction_id) ON DELETE CASCADE,
  audio_uri         text NOT NULL,         -- drive://<fileId> or s3://...
  audio_sha256      text,
  duration_seconds  numeric(10,2),
  sample_rate_hz    int,
  channels          smallint,
  asr_provider      text NOT NULL,         -- 'cohere-transcribe-arabic'
  asr_model_version text NOT NULL,         -- '07-2026'
  asr_confidence    numeric(3,2),
  language          char(2),

  full_text         text NOT NULL,
  -- Per-segment detail: [{seq, start_sec, end_sec, speaker, text, confidence}]
  segments          jsonb NOT NULL DEFAULT '[]'::jsonb,

  -- How speakers were separated. 'none' means every downstream agent score
  -- rests on the LLM guessing who spoke — which must be visible, not implicit.
  diarization       text NOT NULL DEFAULT 'none'
                    CHECK (diarization IN ('none', 'dual_channel', 'pyannote', 'provider', 'manual')),
  speaker_map       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"SPEAKER_00": "agent"}

  transcribed_at    timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON transcripts (asr_provider, asr_model_version);
