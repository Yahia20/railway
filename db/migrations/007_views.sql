-- 007 — reporting views and the nightly rollup.
-- Views hold no state, so they are safe to CREATE OR REPLACE on every deploy.

-- Agent scorecard. Bot-handled conversations are excluded: the bot qualifies
-- customers before a human joins, and counting its conversations against human
-- agents makes every QA number wrong.
CREATE OR REPLACE VIEW v_agent_scorecard AS
SELECT
  a.agent_id,
  a.full_name,
  a.team,
  count(*)                                      AS evaluated_interactions,
  count(*) FILTER (WHERE i.channel = 'phone_call') AS calls,
  count(*) FILTER (WHERE i.channel <> 'phone_call') AS chats,
  round(avg(e.final_score), 1)                  AS avg_score,
  round(avg(e.m1_reception), 1)                 AS avg_reception,
  round(avg(e.m2_offer), 1)                     AS avg_offer,
  round(avg(e.m3_objections), 1)                AS avg_objections,
  round(avg(e.m4_followup), 1)                  AS avg_followup,
  round(avg(e.m5_closing), 1)                   AS avg_closing,
  -- A module scored on very few conversations is not a signal. Surface the n
  -- alongside the mean so nobody coaches against three data points.
  count(e.m5_closing)                           AS n_closing_scored,
  round(avg(m.first_response_seconds))          AS avg_first_response_sec,
  count(*) FILTER (WHERE e.behavior_flags <> '[]'::jsonb) AS flagged_conversations
FROM agent_evaluations e
JOIN interactions i ON i.interaction_id = e.interaction_id
JOIN agents a       ON a.agent_id = e.agent_id
LEFT JOIN interaction_metrics m ON m.interaction_id = e.interaction_id
WHERE a.is_bot = false
GROUP BY a.agent_id, a.full_name, a.team;

-- Does the model score calls worse than chats, or is the transcript just bad?
-- This view is the first thing to look at when QA numbers look odd.
CREATE OR REPLACE VIEW v_quality_by_input AS
SELECT
  e.input_type,
  t.diarization,
  width_bucket(coalesce(t.asr_confidence, 1.0), 0, 1, 5) AS confidence_bucket,
  count(*)                     AS n,
  round(avg(e.final_score), 1) AS avg_score,
  round(stddev_pop(e.final_score), 1) AS score_spread
FROM agent_evaluations e
LEFT JOIN transcripts t ON t.interaction_id = e.interaction_id
GROUP BY e.input_type, t.diarization, confidence_bucket;

-- Promises made vs. promises kept, per agent.
CREATE OR REPLACE VIEW v_followup_discipline AS
SELECT
  a.agent_id,
  a.full_name,
  count(*)                                            AS promises_made,
  count(*) FILTER (WHERE f.status = 'fulfilled')      AS kept,
  count(*) FILTER (WHERE f.status IN ('late','missed')) AS broken,
  count(*) FILTER (WHERE f.status = 'open' AND f.due_at < now()) AS overdue_now,
  round(avg(f.hours_to_fulfil) FILTER (WHERE f.status = 'fulfilled'), 1) AS avg_hours_to_fulfil
FROM follow_ups f
JOIN agents a ON a.agent_id = f.agent_id
GROUP BY a.agent_id, a.full_name;

CREATE OR REPLACE VIEW v_funnel AS
SELECT
  d.category_id,
  d.stage_id,
  d.stage_semantic,
  count(*)          AS deals,
  sum(d.amount)     AS pipeline_value,
  d.currency
FROM deals d
GROUP BY d.category_id, d.stage_id, d.stage_semantic, d.currency;

-- ---------------------------------------------------------------------------
-- The nightly rollup. Fully derived: truncate and rebuild, always.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION rebuild_customer_metrics() RETURNS int
LANGUAGE plpgsql AS $$
DECLARE
  n int;
BEGIN
  TRUNCATE customer_metrics;

  INSERT INTO customer_metrics (
    customer_id, interaction_count, call_count, chat_count,
    first_interaction_at, last_interaction_at, days_since_last_contact,
    deal_count, won_deal_count, lost_deal_count, lifetime_value, open_pipeline_value,
    loyalty_points_balance, active_voucher_value,
    latest_lead_temp, latest_buying_stage, open_follow_ups, computed_at
  )
  SELECT
    c.customer_id,
    coalesce(i.n, 0), coalesce(i.calls, 0), coalesce(i.chats, 0),
    i.first_at, i.last_at,
    CASE WHEN i.last_at IS NULL THEN NULL
         ELSE (now()::date - i.last_at::date) END,
    coalesce(d.n, 0), coalesce(d.won, 0), coalesce(d.lost, 0),
    coalesce(d.won_value, 0), coalesce(d.open_value, 0),
    coalesce(l.balance, 0), coalesce(v.active_value, 0),
    a.lead_temp, a.buying_stage,
    coalesce(f.open_n, 0), now()
  FROM customers c
  LEFT JOIN (
    SELECT customer_id,
           count(*) AS n,
           count(*) FILTER (WHERE channel = 'phone_call')  AS calls,
           count(*) FILTER (WHERE channel <> 'phone_call') AS chats,
           min(started_at) AS first_at,
           max(started_at) AS last_at
    FROM interactions WHERE customer_id IS NOT NULL GROUP BY customer_id
  ) i ON i.customer_id = c.customer_id
  LEFT JOIN (
    SELECT customer_id,
           count(*) AS n,
           count(*) FILTER (WHERE stage_semantic = 'S') AS won,
           count(*) FILTER (WHERE stage_semantic = 'F') AS lost,
           sum(amount) FILTER (WHERE stage_semantic = 'S') AS won_value,
           sum(amount) FILTER (WHERE stage_semantic = 'P') AS open_value
    FROM deals WHERE customer_id IS NOT NULL GROUP BY customer_id
  ) d ON d.customer_id = c.customer_id
  LEFT JOIN (
    SELECT customer_id, sum(points) AS balance
    FROM loyalty_ledger WHERE customer_id IS NOT NULL GROUP BY customer_id
  ) l ON l.customer_id = c.customer_id
  LEFT JOIN (
    SELECT customer_id, sum(face_value) AS active_value
    FROM vouchers WHERE status = 'active' AND customer_id IS NOT NULL GROUP BY customer_id
  ) v ON v.customer_id = c.customer_id
  LEFT JOIN (
    SELECT customer_id, count(*) AS open_n
    FROM follow_ups WHERE status = 'open' AND customer_id IS NOT NULL GROUP BY customer_id
  ) f ON f.customer_id = c.customer_id
  LEFT JOIN LATERAL (
    SELECT ia.lead_temp, ia.buying_stage
    FROM interaction_analysis ia
    JOIN interactions ii ON ii.interaction_id = ia.interaction_id
    WHERE ii.customer_id = c.customer_id
    ORDER BY ii.started_at DESC
    LIMIT 1
  ) a ON true
  WHERE c.is_merged_into IS NULL;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
