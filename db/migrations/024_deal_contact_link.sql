-- 024 — link a deal to a CUSTOMER, not only to a conversation.
--
-- WHAT WAS MISSING. `deals.customer_id` has existed since 003 and has never held
-- a single value. Bitrix links a deal to a CONTACT (`CONTACT_ID`), workflow 04
-- fetched that field and dropped it on the floor, and nothing resolved a Bitrix
-- contact id to the merged customer this system builds.
--
-- The consequence shows up the moment anybody asks a per-customer question. A
-- customer's deals could only be reached through their CONVERSATIONS — the
-- 1,175 threads that carry a `deal_id` — so a deal opened for someone who never
-- chatted was invisible on their profile, and a customer page could show "0
-- deals" for a customer with four.
--
-- THREE PIECES, AND THE ORDER MATTERS.
--
--   1. `deals.bitrix_contact_id`, so the fetched field stops being discarded.
--   2. `customer_identities` rows of kind `bitrix_contact_id`, built from the
--      conversations that already carry both ids. That table is the one place
--      identity claims live, and it is what makes a bad merge discoverable.
--   3. `link_deal_customers()`, resolving 1 through 2 onto `deals.customer_id`.
--
-- Piece 2 is the interesting one. The contact id arrives on `interactions`
-- (01c stores `external_contact_id` from the chat payload) and the customer is
-- resolved on the same row by workflow 03. So the mapping already exists in the
-- data; it has just never been written down anywhere a deal can read it.
--
-- CONFIDENCE 0.95, NOT 1.00. A phone match is an identity assertion about a
-- person. This is an assertion about a Bitrix RECORD, inherited from whatever
-- matched the conversation. Recording it at the same confidence as an exact
-- phone match would make the two indistinguishable in `v_identity_*`, and the
-- one thing that table exists for is telling a strong claim from a weaker one.
--
-- `method` is `exact_crm_id`, which is what the enum has meant since 002 and
-- what this is: an exact match on a CRM record id. It is not `manual` and it is
-- not a guess. Do not add an enum value for it.

BEGIN;

SET lock_timeout = '5s';
SET statement_timeout = '10min';

-- ---------------------------------------------------------------------------
-- 1 · Stop dropping the field
-- ---------------------------------------------------------------------------

ALTER TABLE deals ADD COLUMN IF NOT EXISTS bitrix_contact_id text;

COMMENT ON COLUMN deals.bitrix_contact_id IS
  'CONTACT_ID from crm.deal.list. Bitrix links a deal to a contact, not to our merged customer; customer_id is resolved from this by link_deal_customers().';

CREATE INDEX IF NOT EXISTS deals_bitrix_contact_id_idx
  ON deals (bitrix_contact_id) WHERE bitrix_contact_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2 · link_deal_customers() — idempotent, safe every night
--
-- Two sources, in order of directness:
--
--   a) the contact id, through customer_identities. A deal and a customer that
--      share a Bitrix contact are the same person.
--   b) the conversation. A deal reached by a thread whose customer is known is
--      that customer's deal, and this is the route that already worked.
--
-- (b) runs second and only fills what (a) left empty, so the identity table
-- always wins. Both are guarded by IS DISTINCT FROM, so a night that changes
-- nothing updates nothing.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION link_deal_customers()
RETURNS jsonb
LANGUAGE plpgsql
AS $fn$
DECLARE
  n_ident int;
  n_by_contact int;
  n_by_thread int;
BEGIN
  -- a) Record the contact -> customer mapping the conversations already prove.
  --    One row per (customer, contact): a customer can hold several Bitrix
  --    contacts after a merge, and that is worth keeping rather than collapsing.
  INSERT INTO customer_identities (customer_id, kind, value, method, confidence,
                                   first_seen_at)
  SELECT DISTINCT ON (i.customer_id, i.external_contact_id)
         i.customer_id,
         'bitrix_contact_id',
         i.external_contact_id,
         'exact_crm_id',
         0.95,
         min(i.started_at) OVER (PARTITION BY i.customer_id, i.external_contact_id)
    FROM interactions i
   WHERE i.customer_id IS NOT NULL
     AND i.external_contact_id IS NOT NULL
     AND i.external_contact_id <> ''
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n_ident = ROW_COUNT;

  -- b) The direct route: same Bitrix contact, same person.
  UPDATE deals d
     SET customer_id = ci.customer_id, updated_at = now()
    FROM customer_identities ci
   WHERE ci.kind = 'bitrix_contact_id'
     AND ci.value = d.bitrix_contact_id
     AND d.bitrix_contact_id IS NOT NULL
     AND d.customer_id IS DISTINCT FROM ci.customer_id;
  GET DIAGNOSTICS n_by_contact = ROW_COUNT;

  -- c) The fallback, for deals whose contact we cannot resolve: the customer on
  --    a conversation that carries this deal. Only fills a NULL -- it must
  --    never overwrite (b).
  UPDATE deals d
     SET customer_id = s.customer_id, updated_at = now()
    FROM (
      SELECT i.deal_id, min(i.customer_id::text)::uuid AS customer_id
        FROM interactions i
       WHERE i.deal_id IS NOT NULL AND i.customer_id IS NOT NULL
       GROUP BY i.deal_id
      HAVING count(DISTINCT i.customer_id) = 1     -- ambiguous: leave it NULL
    ) s
   WHERE s.deal_id = d.deal_id
     AND d.customer_id IS NULL;
  GET DIAGNOSTICS n_by_thread = ROW_COUNT;

  RETURN jsonb_build_object(
    'identities_added',  n_ident,
    'linked_by_contact', n_by_contact,
    'linked_by_thread',  n_by_thread);
END
$fn$;

COMMENT ON FUNCTION link_deal_customers() IS
  'Resolve deals.customer_id from the Bitrix contact id, falling back to the conversation that carries the deal. Idempotent; run nightly from workflow 04 after the deal upsert.';

-- ---------------------------------------------------------------------------
-- 3 · v_customer_deal_summary — what a customer profile actually asks for
--
-- Built as a view so the page, the report and any future query all count a
-- "won deal" the same way. `S` is Bitrix's own semantic for won; spelling it
-- out in three renderers is three chances to spell it differently.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_customer_deal_summary AS
SELECT d.customer_id,
       count(*)                                          AS deals,
       count(*) FILTER (WHERE d.stage_semantic = 'S')     AS won,
       count(*) FILTER (WHERE d.stage_semantic = 'F')     AS lost,
       count(*) FILTER (WHERE d.stage_semantic = 'P')     AS open,
       round(sum(d.amount) FILTER (WHERE d.stage_semantic = 'S'), 2) AS won_amount,
       max(d.modified_at_src)                             AS last_deal_at
FROM deals d
WHERE d.customer_id IS NOT NULL
GROUP BY d.customer_id;

COMMENT ON VIEW v_customer_deal_summary IS
  'Per-customer deal counts. S = won, F = lost, P = open, in Bitrix''s own vocabulary. Read this rather than restating the letters.';

-- ---------------------------------------------------------------------------
-- 4 · Report what happened
-- ---------------------------------------------------------------------------

DO $report$
DECLARE r jsonb;
BEGIN
  r := link_deal_customers();
  RAISE NOTICE '024: %', r;
  RAISE NOTICE '024: deals with a customer = % of %',
    (SELECT count(*) FROM deals WHERE customer_id IS NOT NULL),
    (SELECT count(*) FROM deals);
END
$report$;

COMMIT;
