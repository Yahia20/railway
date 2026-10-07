-- 029 — the trip columns pass 1 has been filling into the same black hole.
--
-- FOUND BY TRYING TO COUNT SOMETHING. The client asked a simple question — how
-- many customers gave us a destination, a headcount and a date — and the answer
-- came back ZERO out of 46 analysed conversations. It is not zero. It is 14.
--
-- `interaction_analysis` defines about twenty-five columns describing what the
-- customer asked for. 01d's `Store pass1` writes ten of them:
--
--     interaction_id, schema_version, prompt_version, model, input_type,
--     source_quality, language, intent, summary_ar, confidence,
--     uncertain_fields, raw_response
--
-- `date_start`, `date_end`, `nights`, `travelers_total`, `travelers_adults`,
-- `travelers_children`, `travelers_infants`, `group_count` and
-- `date_flexibility` are in no INSERT anywhere. NULL on all 46 rows. The values
-- were extracted, paid for, and stored in `raw_response` where no report looks.
-- This is the third instance of one bug in this database — `display_name` (027)
-- and `customer_name` (027) were the first two — and the shape is always the
-- same: a column exists, nothing writes it, and no query fails.
--
-- MEASURED IN `raw_response` ACROSS THE 46 ANALYSES:
--
--     destination named        26
--     travellers given         23
--     start date given         15
--     ALL THREE                14      <- the number the dashboard should show
--
-- `interaction_destinations` was empty for the same reason and is filled here
-- too. `destination_id` stays NULL on purpose: that column is the join to a
-- canonical geography dimension, and `raw_name` — "what the customer actually
-- said", which is what 004 called it — is the only honest thing we have. A
-- canonicalisation step can fill the id later without touching these rows.
--
-- `interaction_requests` IS NOT THE ANSWER HERE, and that is worth recording
-- because rule 9 says it should be. It holds 2 rows for 46 analyses: pass 1 v6
-- emits a `requests[]` array only when it sees more than one distinct ask, and
-- on this corpus it almost never does. So the primary-request columns on
-- `interaction_analysis` are, in practice, the conversation. When `requests[]`
-- starts firing, the real-ask predicate already counts BOTH — see report.py.

BEGIN;

SET lock_timeout = '5s';
SET statement_timeout = '10min';

-- ---------------------------------------------------------------------------
-- 1 · The trip, out of the JSON and into its own columns
--
-- coalesce onto the existing value everywhere: this must be re-runnable and
-- must never blank a column somebody filled by hand.
--
-- nullif + a regex guard before every ::int and ::date. pass 1 is a language
-- model writing JSON; "two adults" in a numeric field would abort the whole
-- migration, and one bad row is not a reason to lose the other 45.
-- ---------------------------------------------------------------------------

UPDATE interaction_analysis a
   SET date_start = coalesce(a.date_start,
         CASE WHEN t->>'date_start' ~ '^\d{4}-\d{2}-\d{2}$'
              THEN (t->>'date_start')::date END),
       date_end = coalesce(a.date_end,
         CASE WHEN t->>'date_end' ~ '^\d{4}-\d{2}-\d{2}$'
              THEN (t->>'date_end')::date END),
       nights = coalesce(a.nights,
         CASE WHEN t->>'nights' ~ '^\d{1,4}$' THEN (t->>'nights')::int END),
       date_flexibility = coalesce(a.date_flexibility,
         CASE WHEN t->>'date_flexibility' IN ('fixed', 'flexible', 'unknown')
              THEN t->>'date_flexibility' END),
       group_count = coalesce(a.group_count,
         CASE WHEN t->>'group_count' ~ '^\d{1,4}$' THEN (t->>'group_count')::int END),
       travelers_total = coalesce(a.travelers_total,
         CASE WHEN t->'travelers'->>'total' ~ '^\d{1,4}$'
              THEN (t->'travelers'->>'total')::int END),
       travelers_adults = coalesce(a.travelers_adults,
         CASE WHEN t->'travelers'->>'adults' ~ '^\d{1,4}$'
              THEN (t->'travelers'->>'adults')::int END),
       travelers_children = coalesce(a.travelers_children,
         CASE WHEN t->'travelers'->>'children' ~ '^\d{1,4}$'
              THEN (t->'travelers'->>'children')::int END),
       travelers_infants = coalesce(a.travelers_infants,
         CASE WHEN t->'travelers'->>'infants' ~ '^\d{1,4}$'
              THEN (t->'travelers'->>'infants')::int END)
  FROM (SELECT analysis_id, raw_response->'trip' AS t FROM interaction_analysis) s
 WHERE s.analysis_id = a.analysis_id
   AND s.t IS NOT NULL
   AND jsonb_typeof(s.t) = 'object';

-- ---------------------------------------------------------------------------
-- 2 · Where they wanted to go
--
-- ON CONFLICT on (analysis_id, raw_name, leg_order) makes this idempotent, and
-- an empty `name` is skipped rather than written as '' — raw_name is NOT NULL
-- and a blank destination is not a destination.
-- ---------------------------------------------------------------------------

INSERT INTO interaction_destinations (analysis_id, raw_name, role, leg_order, nights)
SELECT a.analysis_id,
       btrim(d->>'name'),
       CASE WHEN d->>'role' IN ('origin', 'destination', 'stopover', 'excursion')
            THEN d->>'role' ELSE 'destination' END,
       CASE WHEN d->>'leg_order' ~ '^\d{1,3}$' THEN (d->>'leg_order')::int
            ELSE ord::int END,
       CASE WHEN d->>'nights' ~ '^\d{1,4}$' THEN (d->>'nights')::int END
  FROM interaction_analysis a
  CROSS JOIN LATERAL jsonb_array_elements(
         coalesce(a.raw_response->'trip'->'destinations', '[]'::jsonb))
         WITH ORDINALITY AS e(d, ord)
 WHERE nullif(btrim(coalesce(d->>'name', '')), '') IS NOT NULL
ON CONFLICT (analysis_id, raw_name, leg_order) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3 · Report what the dashboard can now count
-- ---------------------------------------------------------------------------

DO $report$
BEGIN
  RAISE NOTICE '029: analyses = %, with a destination row = %, with travellers = %, with a start date = %',
    (SELECT count(*) FROM interaction_analysis),
    (SELECT count(DISTINCT analysis_id) FROM interaction_destinations),
    (SELECT count(*) FROM interaction_analysis WHERE travelers_total IS NOT NULL),
    (SELECT count(*) FROM interaction_analysis WHERE date_start IS NOT NULL);
  RAISE NOTICE '029: REAL ASKS (destination + travellers + date) = %',
    (SELECT count(*) FROM interaction_analysis a
      WHERE a.travelers_total IS NOT NULL
        AND a.date_start IS NOT NULL
        AND EXISTS (SELECT 1 FROM interaction_destinations dd
                     WHERE dd.analysis_id = a.analysis_id
                       AND dd.role = 'destination'));
END
$report$;

COMMIT;
