-- 006 — operational tables: promises the agent made, consent, the nightly
-- customer rollup, and job bookkeeping.

-- ---------------------------------------------------------------------------
-- follow_ups — a promise made in a conversation ("I'll send you the quote"),
-- and whether it was kept.
--
-- This table is what makes Module 4 of the quality rubric meaningful for CALLS.
-- Inside a single phone call there is no follow-up to observe, so a call always
-- scores full marks on follow-up — which inflates 20% of the agent's grade.
-- Scoring follow-up across the customer's TIMELINE instead fixes that, and this
-- is the table that carries it.
-- ---------------------------------------------------------------------------
CREATE TABLE follow_ups (
  follow_up_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid REFERENCES customers(customer_id),
  agent_id         uuid REFERENCES agents(agent_id),
  promised_in      uuid REFERENCES interactions(interaction_id) ON DELETE CASCADE,
  fulfilled_in     uuid REFERENCES interactions(interaction_id) ON DELETE SET NULL,

  promise_text     text,                   -- what the agent actually said
  promised_at      timestamptz NOT NULL,
  due_at           timestamptz,            -- explicit if a timeframe was given
  fulfilled_at     timestamptz,
  hours_to_fulfil  numeric(8,2),
  status           text NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'fulfilled', 'late', 'missed', 'cancelled')),
  channel          channel,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON follow_ups (agent_id, status);
CREATE INDEX ON follow_ups (customer_id, promised_at DESC);
CREATE INDEX ON follow_ups (status, due_at) WHERE status = 'open';
CREATE TRIGGER t_followups_updated BEFORE UPDATE ON follow_ups
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- consents — which regime applies is decided by customers.residence_country.
-- Egyptian PDPL and Saudi PDPL differ; if you operate in both, this table and
-- the encryption policy must satisfy both.
--
-- Call recording consent is not optional: you are storing voice audio.
-- ---------------------------------------------------------------------------
CREATE TABLE consents (
  consent_id   bigserial PRIMARY KEY,
  customer_id  uuid NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
  kind         text NOT NULL CHECK (kind IN ('marketing', 'call_recording',
                                             'data_processing', 'whatsapp')),
  granted      boolean NOT NULL,
  regime       text,                       -- 'EG_PDPL' | 'SA_PDPL' | 'GDPR'
  source       text,                       -- where the consent was captured
  occurred_at  timestamptz NOT NULL,
  evidence_uri text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON consents (customer_id, kind, occurred_at DESC);

-- ---------------------------------------------------------------------------
-- customer_metrics — fully derived, rebuilt nightly, never written by hand.
-- It can always be dropped and recomputed; if a dashboard number is wrong the
-- fix belongs in the events underneath, not here.
-- ---------------------------------------------------------------------------
CREATE TABLE customer_metrics (
  customer_id            uuid PRIMARY KEY REFERENCES customers(customer_id) ON DELETE CASCADE,
  interaction_count      int NOT NULL DEFAULT 0,
  call_count             int NOT NULL DEFAULT 0,
  chat_count             int NOT NULL DEFAULT 0,
  first_interaction_at   timestamptz,
  last_interaction_at    timestamptz,
  days_since_last_contact int,

  deal_count             int NOT NULL DEFAULT 0,
  won_deal_count         int NOT NULL DEFAULT 0,
  lost_deal_count        int NOT NULL DEFAULT 0,
  lifetime_value         numeric(14,2) NOT NULL DEFAULT 0,
  open_pipeline_value    numeric(14,2) NOT NULL DEFAULT 0,

  loyalty_points_balance int NOT NULL DEFAULT 0,
  active_voucher_value   numeric(12,2) NOT NULL DEFAULT 0,

  latest_lead_temp       lead_temperature,
  latest_buying_stage    buying_stage,
  top_destination_id     int REFERENCES destinations(destination_id),
  open_follow_ups        int NOT NULL DEFAULT 0,

  computed_at            timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- job_runs — every pipeline step writes here. Without it, "why is yesterday
-- missing?" has no answer.
-- ---------------------------------------------------------------------------
CREATE TABLE job_runs (
  run_id       bigserial PRIMARY KEY,
  job_name     text NOT NULL,
  status       job_status NOT NULL DEFAULT 'pending',
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  items_in     int NOT NULL DEFAULT 0,
  items_ok     int NOT NULL DEFAULT 0,
  items_failed int NOT NULL DEFAULT 0,
  cost_usd     numeric(10,4),
  error        text,
  detail       jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ON job_runs (job_name, started_at DESC);

-- Idempotency + cost control for every LLM/ASR call. Re-running a prompt on an
-- unchanged input should cost nothing, so the pipeline can be safely retried.
CREATE TABLE model_calls (
  call_id        bigserial PRIMARY KEY,
  interaction_id uuid REFERENCES interactions(interaction_id) ON DELETE CASCADE,
  purpose        text NOT NULL,            -- 'asr' | 'pass1_customer' | 'pass2_agent'
  provider       text NOT NULL,
  model          text NOT NULL,
  prompt_version text,
  input_hash     text NOT NULL,            -- hash of the exact input sent
  prompt_tokens  int,
  output_tokens  int,
  cost_usd       numeric(10,6),
  latency_ms     int,
  succeeded      boolean NOT NULL DEFAULT true,
  error          text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (purpose, input_hash, prompt_version)
);
CREATE INDEX ON model_calls (created_at DESC);
