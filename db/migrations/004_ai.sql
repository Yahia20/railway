-- 004 — what the two AI passes produce.
--
-- Pass 1 (customer) and pass 2 (agent) run as SEPARATE model calls that never
-- see each other's output. If one prompt did both, an angry customer would drag
-- down the agent's score and a high-scoring agent would inflate the forecast.
-- Two calls cost marginally more and give you numbers you can trust.

-- ---------------------------------------------------------------------------
-- interaction_analysis — pass 1. What did this person want, how ready are they
-- to buy, what is stopping them.
-- ---------------------------------------------------------------------------
CREATE TABLE interaction_analysis (
  analysis_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  interaction_id   uuid NOT NULL UNIQUE REFERENCES interactions(interaction_id) ON DELETE CASCADE,

  schema_version   text NOT NULL,
  prompt_version   text NOT NULL,          -- which prompt produced this row
  model            text NOT NULL,          -- 'deepseek-chat'
  input_type       input_type NOT NULL,
  -- A budget of 12,400 typed by the customer is stronger evidence than the same
  -- number read out of a 0.60-confidence transcript. Without this you cannot
  -- filter weak extractions out of a forecast.
  source_quality   numeric(3,2),
  analysed_through_seq int,                -- how far through a growing thread this row got

  language         char(2),
  intent           text,
  service          service_type NOT NULL DEFAULT 'unknown',
  service_raw      text,                   -- the customer's own words, pre-enum

  customer_name        text,
  customer_nationality text,
  residence_city       text,

  date_start        date,
  date_end          date,
  nights            int,
  date_flexibility  text CHECK (date_flexibility IN ('fixed', 'flexible', 'unknown')),

  travelers_total   int,
  travelers_adults  int,
  travelers_children int,
  travelers_infants int,
  group_count       int,                   -- families/sub-groups travelling together

  budget_amount     numeric(12,2),
  budget_currency   char(3),
  buying_stage      buying_stage NOT NULL DEFAULT 'unknown',
  lead_temp         lead_temperature NOT NULL DEFAULT 'unknown',
  is_decision_maker boolean,
  objections        jsonb NOT NULL DEFAULT '[]'::jsonb,
  special_requests  jsonb NOT NULL DEFAULT '[]'::jsonb,

  summary_ar        text,
  confidence        numeric(3,2),
  -- Null is a valid answer. A model forced to fill every field invents a budget;
  -- require explicit null and make it list what it guessed at.
  uncertain_fields  jsonb NOT NULL DEFAULT '[]'::jsonb,

  raw_response      jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON interaction_analysis (buying_stage, lead_temp);
CREATE INDEX ON interaction_analysis (service);
CREATE TRIGGER t_analysis_updated BEFORE UPDATE ON interaction_analysis
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- One row per trip leg, so "Istanbul + Trabzon" is two joinable rows rather
-- than a string nobody can group by.
CREATE TABLE interaction_destinations (
  id             bigserial PRIMARY KEY,
  analysis_id    uuid NOT NULL REFERENCES interaction_analysis(analysis_id) ON DELETE CASCADE,
  destination_id int REFERENCES destinations(destination_id),
  raw_name       text NOT NULL,            -- what the customer actually said
  role           text CHECK (role IN ('origin', 'destination', 'stopover', 'excursion')),
  leg_order      int,
  nights         int,
  UNIQUE (analysis_id, raw_name, leg_order)
);
CREATE INDEX ON interaction_destinations (destination_id);

-- ---------------------------------------------------------------------------
-- agent_evaluations — pass 2. The 5-module rubric from the quality spec.
--
-- Module scores are 0-100 and NULLABLE: null means "this module did not apply
-- to this conversation", which is different from zero. A discovery call that
-- never reached an offer must not be scored as if the agent presented a bad
-- offer. weight_applied records the renormalised denominator actually used.
-- ---------------------------------------------------------------------------
CREATE TABLE agent_evaluations (
  evaluation_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  interaction_id   uuid NOT NULL UNIQUE REFERENCES interactions(interaction_id) ON DELETE CASCADE,
  agent_id         uuid REFERENCES agents(agent_id),

  schema_version   text NOT NULL,
  prompt_version   text NOT NULL,
  rubric_version   text NOT NULL,          -- which revision of the quality rubric
  model            text NOT NULL,
  input_type       input_type NOT NULL,
  source_quality   numeric(3,2),
  analysed_through_seq int,

  final_score       numeric(5,2) CHECK (final_score BETWEEN 0 AND 100),
  performance_level text CHECK (performance_level IN
                     ('Excellent', 'Good', 'Average', 'Below Average')),
  weight_applied    numeric(4,3),          -- sum of weights of non-null modules

  m1_reception   numeric(5,2) CHECK (m1_reception   BETWEEN 0 AND 100),
  m2_offer       numeric(5,2) CHECK (m2_offer       BETWEEN 0 AND 100),
  m3_objections  numeric(5,2) CHECK (m3_objections  BETWEEN 0 AND 100),
  m4_followup    numeric(5,2) CHECK (m4_followup    BETWEEN 0 AND 100),
  m5_closing     numeric(5,2) CHECK (m5_closing     BETWEEN 0 AND 100),

  -- Per-criterion detail, exactly as the rubric defines it.
  breakdown      jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- Every deduction must cite the quote it rests on, or coaching is unfalsifiable.
  evidence       jsonb NOT NULL DEFAULT '[]'::jsonb,

  stage_reached  conversation_stage,
  objections_found jsonb NOT NULL DEFAULT '[]'::jsonb,
  behavior_flags jsonb NOT NULL DEFAULT '[]'::jsonb,   -- defeatist language, ignored customer, ...

  top_strength       text,
  top_weakness       text,
  top_recommendation text,
  notes              text,

  raw_response  jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON agent_evaluations (agent_id, created_at DESC);
CREATE INDEX ON agent_evaluations (final_score);
CREATE INDEX ON agent_evaluations (rubric_version, prompt_version);
CREATE TRIGGER t_eval_updated BEFORE UPDATE ON agent_evaluations
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- interaction_metrics — the numbers you must NEVER ask a model for.
-- Response times, durations, counts, after-hours and language-match are
-- computed from metadata. Ask an LLM to count seconds and it guesses, and your
-- QA numbers drift between runs of the same prompt.
-- ---------------------------------------------------------------------------
CREATE TABLE interaction_metrics (
  interaction_id          uuid PRIMARY KEY REFERENCES interactions(interaction_id) ON DELETE CASCADE,
  first_response_seconds  int,
  median_response_seconds int,
  max_response_gap_seconds int,
  agent_talk_ratio        numeric(4,3),    -- calls only; needs diarization
  customer_message_count  int,
  agent_message_count     int,
  conversation_span_seconds int,
  after_hours             boolean,
  language_matched        boolean,         -- agent replied in the customer's language
  followup_count          int NOT NULL DEFAULT 0,
  hours_to_first_followup numeric(8,2),
  computed_at             timestamptz NOT NULL DEFAULT now()
);
