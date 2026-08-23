-- 011 — the production chat API: the source keys that arrive on every message.
--
-- The API delivers a flat ARRAY of message rows and repeats every
-- conversation-level column on each one:
--
--   dealid, crm_entity_id, contact_id, created_at, updated_at, conversation_id
--
-- A fifty-message thread therefore carries fifty identical copies of the deal
-- id and the contact id. They belong on the interaction, once — not on fifty
-- chat_messages rows, and not in fifty raw_events payloads either.
--
-- Nothing here creates a customer, an agent or a deal. Ingest records the
-- source's own identifiers and stops; RESOLVE (workflow 03) turns them into
-- customer_id / agent_id / deal_id once it has enough to be sure. Inventing a
-- customer row per contact_id would hand the nightly resolver a second copy of
-- a person it is about to match on phone, and a wrong merge is far more
-- expensive than a null.

-- ---------------------------------------------------------------------------
-- Conversation-level: written once per thread.
-- ---------------------------------------------------------------------------
ALTER TABLE interactions
  ADD COLUMN IF NOT EXISTS external_contact_id text,
  ADD COLUMN IF NOT EXISTS external_deal_id    text,
  ADD COLUMN IF NOT EXISTS source_created_at   timestamptz,
  ADD COLUMN IF NOT EXISTS source_updated_at   timestamptz;

COMMENT ON COLUMN interactions.external_contact_id IS
  'Bitrix contact_id exactly as the chat API sent it. NOT resolved to '
  'customer_id here — that is RESOLVE''s job.';
COMMENT ON COLUMN interactions.external_deal_id IS
  'Bitrix deal id (dealid / crm_entity_id) as sent. deal_id is linked only when '
  'a deals row for it already exists; ingest never creates a stub deal, because '
  'a deal with no stage and no amount pollutes every funnel report.';
COMMENT ON COLUMN interactions.source_created_at IS
  'created_at as sent by the chat API — the source record''s own timestamp. '
  'Deliberately separate from started_at, which is derived from the messages we '
  'actually hold and is the only one a metric may use.';
COMMENT ON COLUMN interactions.source_updated_at IS
  'updated_at as sent by the chat API. See source_created_at.';

CREATE INDEX IF NOT EXISTS interactions_external_contact_id_idx
  ON interactions (external_contact_id) WHERE external_contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS interactions_external_deal_id_idx
  ON interactions (external_deal_id) WHERE external_deal_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Message-level: genuinely per-row, so it stays on chat_messages.
-- ---------------------------------------------------------------------------
ALTER TABLE chat_messages
  ADD COLUMN IF NOT EXISTS sender_external_id text,
  ADD COLUMN IF NOT EXISTS content_type       text;

COMMENT ON COLUMN chat_messages.sender_external_id IS
  'sender_id as sent (a Bitrix user id for agent turns). Repeats across a '
  'thread, but it is an attribute of the turn, not of the conversation: the '
  'same thread can be handled by two agents.';
COMMENT ON COLUMN chat_messages.content_type IS
  'As sent: text | image | file | ... Kept so a non-text turn can be counted '
  'and excluded from scoring rather than passed on as an empty turn — an empty '
  'turn still sits between a question and its answer and corrupts every '
  'response-gap metric.';

-- The dedup key is unchanged: UNIQUE (interaction_id, sender, sent_at, body_hash).
-- content_type is deliberately NOT part of it. Two attachments from the same
-- sender in the same second with the same (empty) body are indistinguishable
-- from a redelivery in this payload — there is no field that separates them —
-- so one is dropped. That is the requested behaviour, not an oversight.
