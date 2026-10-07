-- 028 — the destination glued to the customer's name.
--
-- 027 shipped `deal_title_name()` and it was right about the shape and wrong
-- about what comes out of it. Measured against the live `deals` table, the
-- first 25 candidates it produced were:
--
--     Abdullah أذربيجان          حسن احمد القرني فيتنام
--     أحمدد جورجيا               Amira Abd Elnaby Turkey
--     علي جلي تركيا              سلمان العتيبي Thailand
--
-- A Bitrix deal title is not `<name> - <source> - <portal>`. It is
-- `<name> <destination> - <source> - <portal>`, and the destination is welded
-- to the name with a space, so nothing in the title's punctuation separates
-- them. Writing those straight into `customers.display_name` would name 877
-- people after the country they asked about.
--
-- THE SEPARATOR IS FREQUENCY, NOT PUNCTUATION.
--
-- A destination is a trailing word that hundreds of different deals end with.
-- A person's surname is not. Measured over the 1,067 candidates: 51 trailing
-- tokens occur 5 or more times, and every one of them is a country, a region,
-- or a fragment of one:
--
--     Saudi Arabia 82 · Turkey 55 · تركيا 47 · جورجيا 39 · روسيا 38
--     السعودية 35 · أذربيجان 26 · إندونيسيا 24 · مصر 21 · إيطاليا 17 …
--
-- Two of the 51 are not places: '.' and 'الله' (from a prayer somebody typed
-- into the name field). Both are noise too, so stripping them is correct for a
-- different reason. **No surname appears in the list** — not العتيبي, not
-- القحطاني — which is the check that matters in a Gulf customer base, and the
-- reason the threshold can be as low as 5.
--
-- WHY NOT `destinations` / `destination_aliases`. They exist, they are empty,
-- and they are the wrong home. They model real places — canonical name,
-- country code, region, kind — and half of what has to be stripped here is a
-- parse artefact: `والنمسا` is "and Austria", `المتحدة` is the tail of "United
-- Arab Emirates". Seeding those as destination aliases would put fragments in
-- a reference table that `interaction_destinations` joins to. This table is
-- honest about being a list of trailing junk, and nothing else reads it.
--
-- LEARNED, NOT HARDCODED. `refresh_deal_title_noise()` recomputes the list from
-- whatever is in `deals` today, so a new market adds its own countries without
-- a migration. The table is editable: a surname that ever does cross the
-- threshold is one DELETE away from being a name again.

BEGIN;

SET lock_timeout = '5s';
SET statement_timeout = '10min';

-- ---------------------------------------------------------------------------
-- 1 · The vocabulary
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS deal_title_noise (
  token      text PRIMARY KEY,
  n_deals    int,                  -- how many titles ended with it when learned
  learned_at timestamptz NOT NULL DEFAULT now(),
  is_manual  boolean NOT NULL DEFAULT false,  -- added by hand; never auto-pruned
  note       text
);

COMMENT ON TABLE deal_title_noise IS
  'Trailing words in a Bitrix deal title that are not part of the customer name - destinations, mostly. Learned by frequency from deals; edit freely, a hand-added row is kept by refresh_deal_title_noise().';

-- ---------------------------------------------------------------------------
-- 2 · Learn it from the data
--
-- ONE-WORD AND TWO-WORD TAILS, because "Saudi Arabia", "المملكة المتحدة" and
-- "البوسنة والهرسك" are two words and stripping only the last one leaves half a
-- country stuck to the name.
--
-- p_min is the whole safety margin. Lower it and a common surname eventually
-- crosses; raise it and "Poland" (5) stays glued on. 5 was measured, not
-- chosen: at 5 the list is 51 tokens and contains no surname.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION refresh_deal_title_noise(p_min int DEFAULT 5)
RETURNS jsonb
LANGUAGE plpgsql
AS $fn$
DECLARE n_added int;
BEGIN
  WITH heads AS (
    -- The head of the title BEFORE any stripping: everything before the first
    -- " - ", which is where the name and the destination sit together.
    SELECT nullif(btrim(regexp_replace(
             split_part(regexp_replace(coalesce(d.title, ''),
                                       '^\((?:Duplicate|Repeated)\):\s*', '', 'i'),
                        ' - ', 1),
             '\s+', ' ', 'g')), '') AS h
      FROM deals d
     WHERE position(' - ' IN coalesce(d.title, '')) > 0
  ), w AS (
    SELECT h, regexp_split_to_array(h, '\s+') AS parts FROM heads WHERE h IS NOT NULL
  ), tails AS (
    SELECT parts[array_length(parts, 1)] AS token
      FROM w WHERE array_length(parts, 1) >= 2
    UNION ALL
    SELECT parts[array_length(parts, 1) - 1] || ' ' || parts[array_length(parts, 1)]
      FROM w WHERE array_length(parts, 1) >= 3
  ), counted AS (
    SELECT token, count(*) AS n
      FROM tails
     WHERE token IS NOT NULL AND btrim(token) <> ''
     GROUP BY token
    HAVING count(*) >= p_min
  )
  INSERT INTO deal_title_noise (token, n_deals, note)
  SELECT token, n, 'learned by frequency'
    FROM counted
  ON CONFLICT (token) DO UPDATE
    SET n_deals = EXCLUDED.n_deals, learned_at = now();
  GET DIAGNOSTICS n_added = ROW_COUNT;

  RETURN jsonb_build_object('threshold', p_min,
                            'tokens_written', n_added,
                            'tokens_total', (SELECT count(*) FROM deal_title_noise));
END
$fn$;

-- ---------------------------------------------------------------------------
-- 3 · Strip it
--
-- UP TO THREE PASSES. "فرنسا وسويسرا" is two tokens, "Ann Switzerland, Austria"
-- is two more, and one pass would leave the second half welded on. Longest
-- match first, so "Saudi Arabia" wins over "Arabia" and the name does not keep
-- the word "Saudi".
--
-- STABLE, not IMMUTABLE: it reads a table. That rules it out of an index and is
-- fine — it is called over 1,268 deals in a nightly job, not in a hot path.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION strip_trailing_noise(p_text text)
RETURNS text
LANGUAGE plpgsql
STABLE
AS $fn$
DECLARE
  s   text := btrim(coalesce(p_text, ''));
  hit text;
BEGIN
  FOR _i IN 1..3 LOOP
    SELECT n.token INTO hit
      FROM deal_title_noise n
     WHERE lower(s) = lower(n.token)
        OR lower(s) LIKE '% ' || lower(n.token)
     ORDER BY char_length(n.token) DESC
     LIMIT 1;
    EXIT WHEN hit IS NULL;
    s   := btrim(left(s, char_length(s) - char_length(hit)));
    hit := NULL;
    EXIT WHEN s = '';
  END LOOP;
  RETURN nullif(s, '');
END
$fn$;

COMMENT ON FUNCTION strip_trailing_noise(text) IS
  'Remove trailing deal_title_noise tokens (destinations, mostly) from a candidate name. Up to three passes, longest match first.';

-- ---------------------------------------------------------------------------
-- 4 · deal_title_name(), corrected
--
-- Same contract as 027: the customer name, or NULL. Two changes — it strips the
-- trailing destination, and it recognises "(Repeated):" as well as
-- "(Duplicate):", which Bitrix uses for the same thing.
--
-- STABLE now rather than IMMUTABLE, because strip_trailing_noise reads a table.
-- Replacing an IMMUTABLE function with a STABLE one is allowed here only
-- because nothing indexes it; if that ever changes, the vocabulary has to move
-- into the function body instead.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION deal_title_name(p_title text)
RETURNS text
LANGUAGE sql
STABLE
AS $fn$
  WITH head AS (
    SELECT strip_trailing_noise(
             nullif(btrim(regexp_replace(
               split_part(regexp_replace(coalesce(p_title, ''),
                                         '^\((?:Duplicate|Repeated)\):\s*', '', 'i'),
                          ' - ', 1),
               '\s+', ' ', 'g')), '')) AS h
     WHERE position(' - ' IN coalesce(p_title, '')) > 0
  )
  SELECT h FROM head
   WHERE h IS NOT NULL
     AND h !~ '[0-9]'
     AND char_length(h) >= 3
     AND array_length(regexp_split_to_array(h, '\s+'), 1) <= 5
     -- Must contain a letter. "..", "~", "L·L♾️" and friends pass a length
     -- check and are not names.
     AND h ~ '[[:alpha:]؀-ۿ]'
     AND lower(h) !~ '^(?:[.\-–~_/\\*#|]+|guest|test|unknown|n/?a|no ?name|عميل|زائر)$';
$fn$;

COMMENT ON FUNCTION deal_title_name(text) IS
  'The customer name out of a Bitrix deal title, or NULL. Titles are "<name> <destination> - <source> - <portal>": split on the FIRST separator, then strip trailing deal_title_noise. Measured over 17,709 titles.';

-- ---------------------------------------------------------------------------
-- 5 · Learn, then report — still without enabling anything
-- ---------------------------------------------------------------------------

DO $report$
DECLARE r jsonb;
BEGIN
  r := refresh_deal_title_noise();
  RAISE NOTICE '028: %', r;
  RAISE NOTICE '028: deal titles now yield % candidate name(s), % distinct',
    (SELECT count(*) FROM deals WHERE deal_title_name(title) IS NOT NULL),
    (SELECT count(DISTINCT deal_title_name(title)) FROM deals
      WHERE deal_title_name(title) IS NOT NULL);
  RAISE NOTICE '028: they would name % customer(s) who have none (flag still OFF)',
    (SELECT count(DISTINCT d.customer_id)
       FROM deals d JOIN customers c ON c.customer_id = d.customer_id
      WHERE deal_title_name(d.title) IS NOT NULL AND c.display_name IS NULL);
END
$report$;

COMMIT;
