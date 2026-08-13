-- Real tourism inquiries, flagged by pass1 v2 (real_ask.is_real_inquiry).
-- One row per flagged call: who called, when, which products they asked
-- about, and the verbatim quote that proves it. This is the sales follow-up
-- queue; rows only exist where the model could quote the customer's own ask.
CREATE OR REPLACE VIEW v_real_asks AS
SELECT i.interaction_id,
       i.started_at,
       i.customer_phone_e164,
       i.customer_phone_raw,
       a.summary_ar,
       a.raw_response->'real_ask'->'products'                   AS products,
       a.raw_response->'commercial'->>'lead_temperature'        AS lead_temperature,
       a.raw_response->'commercial'->>'buying_stage'            AS buying_stage,
       a.raw_response->'real_ask'->'evidence'->0->>'quote'      AS evidence_quote,
       a.updated_at                                             AS analyzed_at
FROM interaction_analysis a
JOIN interactions i USING (interaction_id)
WHERE (a.raw_response->'real_ask'->>'is_real_inquiry')::boolean IS TRUE
ORDER BY i.started_at DESC;
