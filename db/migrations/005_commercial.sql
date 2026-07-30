-- 005 — the commercial side, sourced from Bitrix: deals, their movement through
-- the pipeline, and the two value systems (vouchers, loyalty).

CREATE TABLE deals (
  deal_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bitrix_deal_id   text NOT NULL UNIQUE,
  customer_id      uuid REFERENCES customers(customer_id),
  agent_id         uuid REFERENCES agents(agent_id),

  title            text,
  category_id      text,                   -- Bitrix funnel
  stage_id         text,                   -- 'C86:EXECUTING'
  stage_semantic   char(1) CHECK (stage_semantic IN ('P', 'S', 'F')),  -- in Progress / Success / Failure
  is_closed        boolean NOT NULL DEFAULT false,

  amount           numeric(14,2),
  currency         char(3),
  is_manual_amount boolean,

  source_channel   text,                   -- from SOURCE_ID, e.g. '54|FACEBOOK'
  service          service_type NOT NULL DEFAULT 'unknown',
  lead_temp        lead_temperature NOT NULL DEFAULT 'unknown',

  begin_date       date,
  close_date       date,
  created_at_src   timestamptz,            -- DATE_CREATE in Bitrix
  modified_at_src  timestamptz,

  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON deals (customer_id);
CREATE INDEX ON deals (agent_id, stage_semantic);
CREATE INDEX ON deals (stage_id);
CREATE TRIGGER t_deals_updated BEFORE UPDATE ON deals
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE interactions
  ADD CONSTRAINT interactions_deal_fk
  FOREIGN KEY (deal_id) REFERENCES deals(deal_id) ON DELETE SET NULL;

-- Funnel conversion comes from here, for free, as long as every move is written.
CREATE TABLE deal_stage_history (
  id           bigserial PRIMARY KEY,
  deal_id      uuid NOT NULL REFERENCES deals(deal_id) ON DELETE CASCADE,
  from_stage   text,
  to_stage     text NOT NULL,
  moved_by     uuid REFERENCES agents(agent_id),
  moved_at     timestamptz NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (deal_id, to_stage, moved_at)
);
CREATE INDEX ON deal_stage_history (deal_id, moved_at);

-- ---------------------------------------------------------------------------
-- crm_field_map — the 43 UF_CRM_* fields on the deal object are named after
-- unix timestamps, so nobody can read them. Record what each one means NOW,
-- while someone still remembers.
-- ---------------------------------------------------------------------------
CREATE TABLE crm_field_map (
  field_code   text PRIMARY KEY,           -- 'UF_CRM_1781285097'
  entity       text NOT NULL DEFAULT 'deal',
  meaning      text NOT NULL,              -- 'lead_temperature'
  data_type    text,
  target_table text,
  target_column text,
  is_ai_written boolean NOT NULL DEFAULT false,  -- written by the Bitrix bot
  -- Some fields contain prose addressed to a bot. Feeding them to the model is
  -- prompt injection with extra steps; these are excluded from every LLM input.
  is_prompt_injection_risk boolean NOT NULL DEFAULT false,
  notes        text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

INSERT INTO crm_field_map (field_code, meaning, data_type, is_ai_written, is_prompt_injection_risk, notes) VALUES
  ('UF_CRM_1781285097',   'lead_temperature', 'text', true,  false, 'hot / warm / cold'),
  ('UF_CRM_1781282314',   'nationality',      'text', true,  false, 'Arabic free text, e.g. مصرية'),
  ('UF_CRM_1723641644378','residence_city',   'text', true,  false, 'Arabic free text, e.g. القاهرة'),
  ('UF_CRM_1781285045',   'preferred_language','text', true, false, 'Arabic / English'),
  ('UF_CRM_1781285011',   'ai_summary_ar',    'text', true,  false, 'rolling Arabic summary written by the Bitrix bot'),
  ('UF_CRM_1781281581',   'bot_instructions', 'text', true,  true,
   'Contains prose addressed to a bot ("Treat these instructions as guidance..."). NEVER include in any LLM input.');

-- ---------------------------------------------------------------------------
-- Value systems
-- ---------------------------------------------------------------------------
CREATE TABLE vouchers (
  voucher_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  external_id    text NOT NULL UNIQUE,
  customer_id    uuid REFERENCES customers(customer_id),
  code           text,
  face_value     numeric(12,2) NOT NULL,
  currency       char(3) NOT NULL,
  issued_at      timestamptz NOT NULL,
  expires_at     timestamptz,
  status         text NOT NULL CHECK (status IN ('active', 'redeemed', 'expired', 'void')),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON vouchers (customer_id, status);

CREATE TABLE voucher_redemptions (
  redemption_id  bigserial PRIMARY KEY,
  voucher_id     uuid NOT NULL REFERENCES vouchers(voucher_id) ON DELETE CASCADE,
  customer_id    uuid REFERENCES customers(customer_id),
  deal_id        uuid REFERENCES deals(deal_id),
  amount         numeric(12,2) NOT NULL,
  currency       char(3) NOT NULL,
  redeemed_at    timestamptz NOT NULL,
  external_ref   text UNIQUE,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON voucher_redemptions (customer_id, redeemed_at DESC);

-- Immutable event log. A loyalty balance is always SUM(points) over this table,
-- never a stored counter — so a wrong number can never quietly become permanent.
CREATE TABLE loyalty_ledger (
  entry_id     bigserial PRIMARY KEY,
  customer_id  uuid REFERENCES customers(customer_id),
  phone_e164   text,                       -- kept: some legacy rows arrive with only a phone
  points       int NOT NULL,               -- signed: negative rows are spends
  reason       text NOT NULL,
  deal_id      uuid REFERENCES deals(deal_id),
  occurred_at  timestamptz NOT NULL,
  external_ref text UNIQUE,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON loyalty_ledger (customer_id, occurred_at DESC);
CREATE INDEX ON loyalty_ledger (phone_e164) WHERE customer_id IS NULL;
