-- 001 — extensions, shared enums, helper functions
-- Everything downstream depends on this file. Run first, run once.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";    -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "pg_trgm";     -- fuzzy name matching in identity resolution
CREATE EXTENSION IF NOT EXISTS "unaccent";    -- destination alias folding

-- ---------------------------------------------------------------------------
-- Enums.
-- These are the "shared enums" the CONFORM step maps free text into. Adding a
-- value later is cheap (ALTER TYPE ... ADD VALUE); removing one is not, so keep
-- them coarse and let the detail live in the accompanying *_raw text column.
-- ---------------------------------------------------------------------------

CREATE TYPE channel AS ENUM ('whatsapp', 'facebook', 'instagram', 'telegram',
                             'webchat', 'phone_call', 'email', 'other');

CREATE TYPE direction AS ENUM ('inbound', 'outbound');

CREATE TYPE speaker_role AS ENUM ('customer', 'agent', 'bot', 'system', 'unknown');

CREATE TYPE input_type AS ENUM ('chat', 'call_transcript');

CREATE TYPE buying_stage AS ENUM ('awareness', 'consideration', 'decision',
                                  'purchased', 'lost', 'unknown');

CREATE TYPE lead_temperature AS ENUM ('hot', 'warm', 'cold', 'unknown');

CREATE TYPE service_type AS ENUM ('package', 'flight', 'hotel', 'cruise', 'visa',
                                  'transfer', 'insurance', 'umrah', 'hajj',
                                  'other', 'unknown');

CREATE TYPE objection_kind AS ENUM ('price_expensive', 'cheaper_elsewhere',
                                    'need_time', 'service_unavailable');

CREATE TYPE conversation_stage AS ENUM ('reception', 'offer_presented',
                                        'negotiation', 'follow_up',
                                        'closing_attempted', 'deal_closed');

CREATE TYPE identity_kind AS ENUM ('phone', 'bitrix_contact_id', 'bitrix_deal_id',
                                   'legacy_customer_id', 'email', 'social_handle');

CREATE TYPE match_method AS ENUM ('exact_phone', 'exact_crm_id', 'legacy_id',
                                  'fuzzy_name', 'manual');

CREATE TYPE job_status AS ENUM ('pending', 'running', 'succeeded', 'failed', 'skipped');

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END $$;

-- Stable hash for idempotency keys. sha256 of the canonical text, hex-encoded.
CREATE OR REPLACE FUNCTION text_hash(t text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT encode(digest(coalesce(t, ''), 'sha256'), 'hex')
$$;

-- Fold a destination or city name for alias matching: lowercase, unaccented,
-- Arabic diacritics and tatweel stripped, alef/ya/ta-marbuta normalised,
-- whitespace collapsed. "القاهرة", "القاهره" and "Cairo " must all compare equal
-- to their canonical row, or demand reporting fragments across spellings.
CREATE OR REPLACE FUNCTION fold_name(t text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT btrim(regexp_replace(
           translate(
             lower(unaccent(regexp_replace(t, '[ً-ْـ]', '', 'g'))),
             'أإآىة', 'ااايه'
           ),
           '\s+', ' ', 'g'))
$$;
