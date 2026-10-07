-- 027 — a customer has a NAME.
--
-- WHAT WAS MISSING. `customers.display_name` has existed since 002 and has
-- never held a single value, and neither has `customers.name_source` beside it.
-- 002 did not forget the problem: it wrote the CHECK
--
--     name_source IN ('crm_contact', 'deal_title', 'ai_extracted', 'manual')
--
-- which is a PRECEDENCE, spelled out before anything could fill it, with a
-- comment naming the exact conflict it was built for — a deal titled
-- "Ahmed Foad" whose own comments say "العميلة جنة". The column list was right.
-- Nothing was ever wired to it.
--
-- So every customer page, every dashboard row and every follow-up queue
-- identifies a human being by their phone number. That is not a rendering bug.
-- All three sources of a name are already in this database or already arriving
-- over a wire we pay for, and each is dropped at a different step:
--
--   crm_contact   `crm.contact.list` carries NAME / LAST_NAME. The worker asked
--                 for select=["ID","PHONE"], so the name was never requested —
--                 gotcha 16, a field nobody asked for arrives NULL rather than
--                 as an error — and workflow 04's pairing node reads `c.PHONE`
--                 and drops the rest of the object on the floor.
--
--   deal_title    `deals.title` often IS the customer's name, and since 024 a
--                 deal knows its customer. Often, not always: a title is free
--                 text a salesperson typed, and a package name is not a person.
--                 Hence p_use_deal_title, default FALSE. See section 4.
--
--   ai_extracted  pass 1 extracts `customer.name` and has a careful rule for it
--                 (prompt v6 section 6: whoever greets with a company name is
--                 the AGENT), and it lands in `interaction_analysis.raw_response`.
--                 `interaction_analysis.customer_name` — the column meant to
--                 hold it — is in no INSERT in any workflow. The value is
--                 stored and unreadable, which is the worst of both.
--
-- A WRONG NAME IS WORSE THAN NO NAME, and the prompt already says so in those
-- words. A fabricated name creates a person who does not exist and identity
-- resolution then merges real people onto them. Everything below is therefore
-- rank-ordered and never overwrites a better source with a worse one, `manual`
-- outranks all three so a human correction is permanent, and the one source
-- that is a guess is off by default.

BEGIN;

SET lock_timeout = '5s';
SET statement_timeout = '10min';

-- ---------------------------------------------------------------------------
-- 1 · bitrix_contacts — stop discarding the CRM's own answer
--
-- A table and not a column on `customers`, for the same reason `deals` is a
-- table: this is what BITRIX says, recorded as Bitrix said it, separate from
-- what we concluded. When the two disagree the disagreement has to survive, or
-- there is nothing to audit a bad name against.
--
-- Workflow 04 already calls crm.contact.list every night and already holds
-- these objects in memory for one node. This is where they land.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bitrix_contacts (
  bitrix_contact_id text PRIMARY KEY,
  name              text,
  second_name       text,
  last_name         text,
  -- Rendered once, here, rather than in every reader. Three renderers of
  -- "first + last" is three chances to render it differently.
  --
  -- NOT concat_ws, WHICH WOULD NOT COMPILE. `concat` and `concat_ws` are
  -- STABLE, not IMMUTABLE — they call each argument's output function, and for
  -- some types that depends on a session setting — so Postgres refuses them in
  -- a generated column with "generation expression is not immutable". Every
  -- function below (coalesce, ||, regexp_replace, btrim, nullif) is immutable
  -- over text. regexp_replace is what collapses the double space a missing
  -- middle name would otherwise leave behind.
  full_name         text GENERATED ALWAYS AS (
    nullif(btrim(regexp_replace(
      coalesce(name, '') || ' ' || coalesce(second_name, '') || ' '
                         || coalesce(last_name, ''),
      '\s+', ' ', 'g')), '')
  ) STORED,
  phone_raw         text,
  fetched_at        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE bitrix_contacts IS
  'What crm.contact.list returned, as it returned it. The CRM answer, kept separate from customers.display_name which is our conclusion.';

CREATE INDEX IF NOT EXISTS bitrix_contacts_full_name_idx
  ON bitrix_contacts (full_name) WHERE full_name IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2 · The column pass 1 has been filling into a black hole
--
-- `interaction_analysis.customer_name` (004) has no writer. The value is in
-- `raw_response->'customer'->>'name'` on every row pass 1 ever wrote, so this
-- backfill is a read of data we already paid a model to produce. Going forward
-- 01d's `Store pass1` writes the column directly.
--
-- residence_city and nationality come from the same JSON object in the same
-- statement. Their columns are empty for exactly the same reason and filling
-- them costs nothing extra.
-- ---------------------------------------------------------------------------

UPDATE interaction_analysis
   SET customer_name        = coalesce(customer_name,
                                nullif(btrim(raw_response->'customer'->>'name'), '')),
       customer_nationality = coalesce(customer_nationality,
                                nullif(btrim(raw_response->'customer'->>'nationality'), '')),
       residence_city       = coalesce(residence_city,
                                nullif(btrim(raw_response->'customer'->>'residence_city'), ''))
 WHERE raw_response ? 'customer'
   AND (customer_name IS NULL OR customer_nationality IS NULL OR residence_city IS NULL);

-- ---------------------------------------------------------------------------
-- 3 · The precedence, as data
--
-- A table and not a CASE, so changing which source wins is an UPDATE and not a
-- deploy — the same reasoning as provider_budgets and alert_rules. LOWER rank
-- wins; `manual` is 0 because a human who corrected a name has looked at
-- something no automated source can see.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS name_source_rank (
  name_source text PRIMARY KEY
              CHECK (name_source IN ('crm_contact', 'deal_title',
                                     'ai_extracted', 'manual')),
  rank        int  NOT NULL,
  notes       text
);

INSERT INTO name_source_rank (name_source, rank, notes) VALUES
  ('manual',       0, 'A human corrected it. Nothing automated may overwrite this.'),
  ('crm_contact',  1, 'crm.contact.list NAME/LAST_NAME. The CRM record for this person.'),
  ('deal_title',   2, 'deals.title. Free text a salesperson typed; often a name, sometimes a trip.'),
  ('ai_extracted', 3, 'pass 1 customer.name, read out of the conversation itself.')
ON CONFLICT (name_source) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4 · deal_title_name() — what a deal title actually looks like
--
-- MEASURED ON 17,709 REAL DEAL TITLES, not guessed. The first version of this
-- migration filtered on word count and was WRONG IN THE WORST DIRECTION: it
-- rejected the real names and kept the junk. Both facts are worth writing down
-- because neither is visible from the schema.
--
-- THE SHAPE IS THREE FIELDS, NOT ONE:
--
--     <what the customer typed> - <source channel> - <portal>
--     Abu Hassoun  -  WhatsApp API Bureau Gupshup  -  Travel Gate
--
-- 96% of titles carry the separator, and most carry it TWICE. So the name is
-- everything before the FIRST " - ". Splitting on the last one — the obvious
-- reading — returns "Abu Hassoun - WhatsApp API Bureau Gupshup", which is seven
-- words, which a word-count filter then rejects as "a sentence". Meanwhile
-- ". - Travel Gate" is four words and sails through. Measured: the word-count
-- rule accepted 6% of titles and they were mostly the placeholders.
--
-- SPLIT ON THE FIRST SEPARATOR AND THE NUMBERS INVERT:
--
--     84%  usable as a name      (14,933 rows, 12,423 distinct)
--      3%  contain digits
--      3%  placeholders: '.', '..', '~', 'Guest', '(Duplicate): .'
--      3%  no separator at all
--      3%  under three characters
--      1%  still too long
--
-- REPEATS ARE NOT A PROBLEM HERE. 22% of the usable rows share their text with
-- another deal, which is what happens in a Gulf customer base: two people
-- really are both called محمد أحمد. The name is assigned per customer_id and
-- never used to MERGE two customers — `customer_identities` does that, on
-- phones and CRM ids — so a shared name costs nothing.
--
-- WHAT THIS STILL CANNOT DO, AND WHY THE FLAG STAYS OFF. It cannot tell a
-- person from an organisation: "Dunes Exhibition" passes every test above. A
-- title is free text a human typed, and 84% is a good number and not a
-- guarantee. `resolve_customer_names(true)` turns it on, ranked BELOW the CRM
-- contact, and is an argument rather than a deploy. Look first:
--
--   SELECT * FROM v_customer_name_candidates WHERE source = 'deal_title';
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION deal_title_name(p_title text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $fn$
  WITH head AS (
    SELECT nullif(btrim(regexp_replace(
             split_part(regexp_replace(coalesce(p_title, ''),
                                       '^\(Duplicate\):\s*', '', 'i'),
                        ' - ', 1),
             '\s+', ' ', 'g')), '') AS h
     -- The separator has to be THERE. Without it split_part returns the whole
     -- string, and a title with no structure is not a title we understand.
     WHERE position(' - ' IN coalesce(p_title, '')) > 0
  )
  SELECT h FROM head
   WHERE h !~ '[0-9]'
     AND char_length(h) >= 3
     AND array_length(regexp_split_to_array(h, '\s+'), 1) <= 5
     -- The placeholders a CRM accumulates when a form is submitted empty.
     AND lower(h) !~ '^(?:[.\-–~_/\\*#|]+|guest|test|unknown|n/?a|no ?name|عميل|زائر)$';
$fn$;

COMMENT ON FUNCTION deal_title_name(text) IS
  'The customer name out of a Bitrix deal title, or NULL. Titles are "<name> - <source> - <portal>"; split on the FIRST separator. Measured over 17,709 titles: 84% usable. Never merges customers - a shared name is normal.';

-- ---------------------------------------------------------------------------
-- 5 · resolve_customer_names() — idempotent, safe every night
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION resolve_customer_names(p_use_deal_title boolean DEFAULT false)
RETURNS jsonb
LANGUAGE plpgsql
AS $fn$
DECLARE
  n_crm  int := 0;
  n_deal int := 0;
  n_ai   int := 0;
BEGIN
  -- a) crm_contact — the CRM record for this person, reached through the
  --    contact->customer identities 024 records. Highest automated rank.
  WITH best AS (
    SELECT DISTINCT ON (ci.customer_id)
           ci.customer_id, bc.full_name
      FROM customer_identities ci
      JOIN bitrix_contacts bc ON bc.bitrix_contact_id = ci.value
     WHERE ci.kind = 'bitrix_contact_id'
       AND bc.full_name IS NOT NULL
     ORDER BY ci.customer_id, ci.confidence DESC, bc.fetched_at DESC
  )
  UPDATE customers c
     SET display_name = best.full_name,
         name_source  = 'crm_contact'
    FROM best
   WHERE best.customer_id = c.customer_id
     AND c.display_name IS DISTINCT FROM best.full_name
     -- Never demote. A name already carrying an equal or better source stays.
     AND coalesce((SELECT rank FROM name_source_rank r
                    WHERE r.name_source = c.name_source), 99) > 1;
  GET DIAGNOSTICS n_crm = ROW_COUNT;

  -- b) deal_title — off unless asked for. See section 4 for the measurement.
  IF p_use_deal_title THEN
    WITH best AS (
      SELECT DISTINCT ON (d.customer_id)
             d.customer_id, deal_title_name(d.title) AS title
        FROM deals d
       WHERE d.customer_id IS NOT NULL
         AND deal_title_name(d.title) IS NOT NULL
       ORDER BY d.customer_id, d.modified_at_src DESC NULLS LAST
    )
    UPDATE customers c
       SET display_name = best.title,
           name_source  = 'deal_title'
      FROM best
     WHERE best.customer_id = c.customer_id
       AND c.display_name IS DISTINCT FROM best.title
       AND coalesce((SELECT rank FROM name_source_rank r
                      WHERE r.name_source = c.name_source), 99) > 2;
    GET DIAGNOSTICS n_deal = ROW_COUNT;
  END IF;

  -- c) ai_extracted — what the customer was called in their own conversation.
  --    Lowest rank of the three, and the only one that costs a model call, so
  --    it fills what the CRM could not rather than competing with it.
  --
  --    NOT `uncertain`. Pass 1 puts "customer.name" in uncertain_fields when it
  --    could not tell the two speakers apart, and a name it is unsure of is the
  --    exact case the prompt says is worse than no name at all.
  WITH best AS (
    SELECT DISTINCT ON (i.customer_id)
           i.customer_id, nullif(btrim(ia.customer_name), '') AS nm
      FROM interaction_analysis ia
      JOIN interactions i ON i.interaction_id = ia.interaction_id
     WHERE i.customer_id IS NOT NULL
       AND nullif(btrim(ia.customer_name), '') IS NOT NULL
       AND NOT coalesce(ia.uncertain_fields @> '["customer.name"]'::jsonb, false)
     ORDER BY i.customer_id, i.started_at DESC
  )
  UPDATE customers c
     SET display_name = best.nm,
         name_source  = 'ai_extracted'
    FROM best
   WHERE best.customer_id = c.customer_id
     AND c.display_name IS DISTINCT FROM best.nm
     AND coalesce((SELECT rank FROM name_source_rank r
                    WHERE r.name_source = c.name_source), 99) > 3;
  GET DIAGNOSTICS n_ai = ROW_COUNT;

  RETURN jsonb_build_object(
    'named_from_crm_contact',  n_crm,
    'named_from_deal_title',   n_deal,
    'named_from_ai_extracted', n_ai,
    'deal_title_enabled',      p_use_deal_title);
END
$fn$;

COMMENT ON FUNCTION resolve_customer_names(boolean) IS
  'Fill customers.display_name from the CRM contact, optionally the deal title, then pass 1. Rank-ordered by name_source_rank; never demotes, never overwrites manual. Idempotent; run nightly from workflow 03 after identity resolution.';

-- ---------------------------------------------------------------------------
-- 6 · What a name WOULD be, before anything writes it
--
-- Every source for every customer, side by side, so a disagreement is a row you
-- can read rather than a mystery in a dashboard cell. This is also how you look
-- at deal titles before turning them on.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_customer_name_candidates AS
SELECT customer_id, source, candidate, rank
FROM (
  SELECT ci.customer_id, 'crm_contact'::text AS source, bc.full_name AS candidate, 1 AS rank
    FROM customer_identities ci
    JOIN bitrix_contacts bc ON bc.bitrix_contact_id = ci.value
   WHERE ci.kind = 'bitrix_contact_id' AND bc.full_name IS NOT NULL
  UNION ALL
  SELECT d.customer_id, 'deal_title', deal_title_name(d.title), 2
    FROM deals d
   WHERE d.customer_id IS NOT NULL AND deal_title_name(d.title) IS NOT NULL
  UNION ALL
  SELECT i.customer_id, 'ai_extracted', btrim(ia.customer_name), 3
    FROM interaction_analysis ia
    JOIN interactions i ON i.interaction_id = ia.interaction_id
   WHERE i.customer_id IS NOT NULL
     AND nullif(btrim(ia.customer_name), '') IS NOT NULL
) s
WHERE candidate IS NOT NULL;

COMMENT ON VIEW v_customer_name_candidates IS
  'Every name each source would give each customer. Read this before enabling deal_title, and when two sources disagree.';

-- Coverage, so "no name" is a number on the report rather than an impression.
CREATE OR REPLACE VIEW v_customer_name_coverage AS
SELECT coalesce(c.name_source, 'none')                      AS name_source,
       count(*)                                             AS customers,
       count(*) FILTER (WHERE c.display_name IS NOT NULL)   AS named
FROM customers c
GROUP BY coalesce(c.name_source, 'none')
ORDER BY customers DESC;

-- ---------------------------------------------------------------------------
-- 7 · Report what happened
--
-- crm_contact will be 0 on this first run and that is expected, not a failure:
-- `bitrix_contacts` is empty until workflow 04 next pulls the contacts with
-- NAME in the select list. ai_extracted fills immediately, from analyses we
-- already have.
-- ---------------------------------------------------------------------------

DO $report$
DECLARE r jsonb;
BEGIN
  r := resolve_customer_names();
  RAISE NOTICE '027: %', r;
  RAISE NOTICE '027: customers with a name = % of %',
    (SELECT count(*) FROM customers WHERE display_name IS NOT NULL),
    (SELECT count(*) FROM customers);
  RAISE NOTICE '027: analyses carrying a customer_name = % of %',
    (SELECT count(*) FROM interaction_analysis WHERE customer_name IS NOT NULL),
    (SELECT count(*) FROM interaction_analysis);
  -- What deal titles WOULD add, printed without adding it. The decision to
  -- switch them on should be made against a number, not a feeling.
  RAISE NOTICE '027: deal titles would name % more customer(s) (flag is OFF)',
    (SELECT count(DISTINCT d.customer_id)
       FROM deals d
       JOIN customers c ON c.customer_id = d.customer_id
      WHERE d.customer_id IS NOT NULL
        AND deal_title_name(d.title) IS NOT NULL
        AND c.display_name IS NULL);
END
$report$;

COMMIT;
