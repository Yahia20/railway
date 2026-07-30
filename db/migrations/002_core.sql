-- 002 — the people: customers, the identities that resolve to them, agents,
-- and the destination dimension.

-- ---------------------------------------------------------------------------
-- customers — one row per real human.
-- The surrogate uuid is the foreign key everywhere else. Phone stays the natural
-- key you MATCH on (in customer_identities) but never the key you JOIN on:
-- numbers get reassigned, and a migration later costs far more than a uuid now.
-- ---------------------------------------------------------------------------
CREATE TABLE customers (
  customer_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name       text,
  -- Which source won the name, so a later conflict is auditable rather than
  -- mysterious. Deal 13682 is titled "Ahmed Foad" while its own comments say
  -- "العميلة جنة" — precedence has to be recorded, not guessed.
  name_source        text CHECK (name_source IN ('crm_contact', 'deal_title',
                                                 'ai_extracted', 'manual')),
  primary_phone_e164 text UNIQUE,
  email              text,
  nationality        text,
  residence_city     text,
  residence_country  char(2),          -- ISO-3166-1 alpha-2; decides which privacy regime applies
  preferred_language char(2),          -- ISO-639-1
  first_seen_at      timestamptz NOT NULL DEFAULT now(),
  last_seen_at       timestamptz,
  is_merged_into     uuid REFERENCES customers(customer_id),  -- set when this row loses a merge
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT phone_is_e164 CHECK (primary_phone_e164 IS NULL
                                  OR primary_phone_e164 ~ '^\+[1-9][0-9]{6,14}$')
);
CREATE INDEX ON customers (last_seen_at DESC);
CREATE INDEX ON customers USING gin (display_name gin_trgm_ops);
CREATE TRIGGER t_customers_updated BEFORE UPDATE ON customers
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- customer_identities — every key we have ever seen for a person, and how
-- confident we are that it is them. This table IS the identity-resolution audit
-- log; without it a bad merge is undiscoverable.
-- ---------------------------------------------------------------------------
CREATE TABLE customer_identities (
  identity_id   bigserial PRIMARY KEY,
  customer_id   uuid NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
  kind          identity_kind NOT NULL,
  value         text NOT NULL,
  method        match_method NOT NULL,
  confidence    numeric(3,2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, value)          -- one identifier can only belong to one person
);
CREATE INDEX ON customer_identities (customer_id);

-- ---------------------------------------------------------------------------
-- agents — the people being scored. Sourced from Bitrix users; the phone
-- extension links a call recording's queue/extension back to a human.
-- ---------------------------------------------------------------------------
CREATE TABLE agents (
  agent_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bitrix_user_id  text UNIQUE,
  phone_extension text UNIQUE,          -- e.g. "3009" from q-3009-...wav
  full_name       text NOT NULL,
  team            text,
  is_bot          boolean NOT NULL DEFAULT false,
  is_active       boolean NOT NULL DEFAULT true,
  hired_at        date,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER t_agents_updated BEFORE UPDATE ON agents
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- The AI bot that qualifies customers before a human joins is an "agent" for
-- attribution purposes but must never appear in human QA leaderboards.
INSERT INTO agents (full_name, is_bot, bitrix_user_id)
VALUES ('Bitrix qualification bot', true, 'bot');

-- ---------------------------------------------------------------------------
-- destinations — the dimension that stops "كروز", "Cruise" and "cruise" from
-- becoming three rows in a report. aliases is the folded-name lookup.
-- ---------------------------------------------------------------------------
CREATE TABLE destinations (
  destination_id serial PRIMARY KEY,
  canonical_name text NOT NULL UNIQUE,
  country_code   char(2),
  region         text,
  kind           text CHECK (kind IN ('city', 'country', 'region', 'route', 'sea')),
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE destination_aliases (
  alias_id       serial PRIMARY KEY,
  destination_id int NOT NULL REFERENCES destinations(destination_id) ON DELETE CASCADE,
  alias          text NOT NULL,
  alias_folded   text GENERATED ALWAYS AS (fold_name(alias)) STORED,
  lang           char(2),
  UNIQUE (destination_id, alias)
);
CREATE UNIQUE INDEX ON destination_aliases (alias_folded);
