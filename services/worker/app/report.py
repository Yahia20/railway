"""The operational report, as SQL over the views that already exist.

WHAT THIS REPLACES. Two scripts (`build_dashboard_data.py`,
`build_crm_pages_data.py`) that a person ran by hand against an SSH tunnel and
that wrote a JSON file next to a hand-built HTML page. The last build was
2026-09-01, `local-reports/` is gitignored, and nothing scheduled ever ran them.
A report nobody can see is a report that does not exist.

WHY EVERY PANEL IS SEPARATE AND SEPARATELY FALLIBLE. These queries read across
migrations 007, 015 and 017. A worker deployed against a database one migration
behind must still show the panels that do work, and name the one that does not —
a dashboard that returns 500 because `interaction_requests` is missing tells you
nothing about the pipeline that is running fine. So each panel is caught
individually and reports its own error.

THE ORDER IS THE ARGUMENT. `reconciliation` is first because
`crm_missing_deals` — a request with a verbatim customer quote that nobody
opened a deal for — is the finding this project exists to produce. Everything
below it is context for trusting that number: is the pipeline current, what did
it cost, and is the input good enough to grade.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from . import db

log = logging.getLogger("worker.report")

# How many individual conversations to list under a headline count. The number
# is the finding; the list is the evidence for it, and a browser does not need
# ten thousand rows to make the point.
SAMPLE_LIMIT = 50

# THIS REPORT DESCRIBES CHATS. THERE IS NOTHING ELSE LEFT TO DESCRIBE.
#
# Calls were removed from the pipeline on 2026-09-14 — the lane, its tables and
# its 830 transcripts. It was never the volume that made them unusable: it was
# that all 1,119 recordings decoded to extension 3009, a QUEUE, so no call ever
# carried an `agent_id` and none ever could. 834 evaluations of which ~800 were
# unattributable sat under a headline about agent performance, describing a
# population nobody could act on.
#
# The `input_type = 'chat'` predicates below therefore look redundant, and today
# they are. They stay because they are cheap and because the day a second
# channel arrives is the day someone needs them to already be there — the
# earlier version of this file had no predicate at all, and that is exactly how
# 800 calls got counted as agent performance for a month.
_CHAT_ONLY = " AND e.input_type = 'chat'"


# ---------------------------------------------------------------------------
# 1 · Reconciliation — the finding
# ---------------------------------------------------------------------------

SQL_VERDICTS = """
SELECT verdict,
       count(*)                                   AS conversations,
       sum(ai_request_count)                      AS ai_requests,
       sum(bitrix_deal_count)                     AS bitrix_deals,
       sum(unlogged_request_count)                AS unlogged_requests,
       sum(unlogged_budget)                       AS unlogged_budget
FROM v_request_reconciliation
GROUP BY verdict
ORDER BY conversations DESC
"""

# Only conversations where the model found MORE evidence-backed requests than
# the CRM holds deals. Ordered by money first, because that is the order a sales
# manager would work the list in.
SQL_MISSING_DEALS = """
SELECT external_id,
       channel::text                              AS channel,
       started_at,
       primary_bitrix_deal,
       bitrix_deal_count,
       ai_request_count,
       unlogged_request_count,
       unlogged_budget
FROM v_request_reconciliation
WHERE verdict = 'crm_missing_deals'
ORDER BY coalesce(unlogged_budget, 0) DESC, unlogged_request_count DESC,
         started_at DESC
LIMIT %(limit)s
"""

# What the unlogged requests actually ASK FOR. A count of missed opportunities
# is an argument; the customer's own words are what makes somebody act on it.
SQL_UNLOGGED_REQUESTS = """
SELECT i.external_id,
       r.seq,
       r.service::text                            AS service,
       r.service_raw,
       r.intent,
       r.destination,
       r.budget_amount,
       r.budget_currency,
       r.outcome,
       r.evidence->0->>'quote'                    AS quote
FROM interaction_requests r
JOIN interactions i USING (interaction_id)
WHERE r.matched_bitrix_deal_id IS NULL
  AND r.evidence_valid IS NOT FALSE
ORDER BY coalesce(r.budget_amount, 0) DESC, i.started_at DESC
LIMIT %(limit)s
"""


# ---------------------------------------------------------------------------
# 2 · Pipeline health — is the number above current?
# ---------------------------------------------------------------------------

SQL_CHAT_JOBS = """
SELECT status,
       count(*)                                   AS n,
       max(updated_at)                            AS last_moved,
       count(*) FILTER (WHERE last_error IS NOT NULL) AS with_error
FROM chat_eval_jobs GROUP BY status ORDER BY n DESC
"""

# Alert evaluation runs AFTER the job is terminal, and a terminal job is never
# claimed again -- so a throw in that node loses the whole tick's alerts
# permanently and silently. The stamp is written in the same statement as the
# evaluation, which makes the gap countable: a judged thread with no stamp is
# work that was dropped, not work not yet due. Should always read zero.
SQL_ALERTS_NOT_EVALUATED = """
SELECT count(*)          AS threads_missing_alerts,
       min(evaluated_at) AS oldest,
       max(evaluated_at) AS newest
FROM v_chat_alerts_pending
"""

# Money, and whether the pipeline is allowed to spend any. A stopped pipeline
# and a broken one look identical from the outside, so the reason has to be a
# panel rather than something you infer from an empty queue.
SQL_BUDGET_GATE = """
SELECT provider, may_run, reason, spend_mtd_usd, monthly_cap_usd,
       remaining_usd, balance_usd, checked_at
FROM v_pipeline_gate ORDER BY may_run, provider
"""

SQL_SPEND = """
SELECT provider, component, calls, failed, output_tokens, cached_tokens,
       spend_usd, at_peak_rate, last_at
FROM v_spend_by_component ORDER BY spend_usd DESC NULLS LAST
"""

# The one part of the Bitrix integration that is not automated: `user.get` is
# outside the webhook's scope, so a salesperson who joins after the roster was
# built owns deals under a Bitrix id nobody has named. Silent otherwise.
SQL_ROSTER_GAPS = """
SELECT bitrix_user_id, deals, first_deal_at, latest_deal_at
FROM v_roster_gaps LIMIT 25
"""

SQL_DUE_NOW = """
SELECT count(*)                                   AS threads_due,
       min(idle_days)                             AS min_idle_days,
       max(idle_days)                             AS max_idle_days
FROM v_chat_eval_due
"""

SQL_INGEST_FRESHNESS = """
SELECT external_source,
       count(*)                                   AS interactions,
       max(started_at)                            AS newest_conversation,
       count(*) FILTER (
         WHERE interaction_id IN (SELECT interaction_id FROM interaction_analysis)
       )                                          AS analysed
FROM interactions
GROUP BY external_source
ORDER BY interactions DESC
"""


# ---------------------------------------------------------------------------
# 3 · Cost — what the judging actually billed
# ---------------------------------------------------------------------------

# `model_calls` is the only measurement of what this system costs. Splitting by
# `priced_at_peak` is what makes a timezone mistake visible: identical work
# billed at double rate shows up here and nowhere else.
SQL_COST = """
SELECT purpose,
       count(*)                                   AS calls,
       count(*) FILTER (WHERE NOT succeeded)      AS failed,
       sum(prompt_tokens)                         AS prompt_tokens,
       sum(cached_tokens)                         AS cached_tokens,
       sum(output_tokens)                         AS output_tokens,
       round(sum(cost_usd), 4)                    AS cost_usd,
       count(*) FILTER (WHERE priced_at_peak)     AS at_peak,
       round(sum(cost_usd) FILTER (WHERE priced_at_peak), 4) AS cost_at_peak,
       round(avg(latency_ms))                     AS avg_latency_ms
FROM model_calls
WHERE created_at >= now() - make_interval(days => %(days)s)
GROUP BY purpose ORDER BY cost_usd DESC NULLS LAST
"""

SQL_COST_BY_DAY = """
SELECT (created_at AT TIME ZONE 'Asia/Riyadh')::date AS day,
       count(*)                                   AS calls,
       round(sum(cost_usd), 4)                    AS cost_usd,
       count(*) FILTER (WHERE priced_at_peak)     AS at_peak
FROM model_calls
WHERE created_at >= now() - make_interval(days => %(days)s)
GROUP BY 1 ORDER BY 1
"""

# When judging actually ran, in Riyadh hours. The schedule claims 23:00-03:59;
# if n8n is resolving those crons in another timezone this histogram is the
# proof, and it is the only place in the system that would show it.
SQL_JUDGE_HOURS = """
SELECT extract(hour FROM created_at AT TIME ZONE 'Asia/Riyadh')::int AS riyadh_hour,
       count(*)                                   AS calls,
       count(*) FILTER (WHERE priced_at_peak)     AS at_peak
FROM model_calls
WHERE created_at >= now() - make_interval(days => %(days)s)
GROUP BY 1 ORDER BY 1
"""


# ---------------------------------------------------------------------------
# 4 · Quality — is the input good enough to grade?
# ---------------------------------------------------------------------------

# THE HEADLINE MEAN IS NOT A BARE KEY, ON PURPOSE.
#
# The owner's decision is that a score is ALWAYS shown, with its sample size
# and the interim-method caveat beside it. `score_display` is one jsonb object
# holding the number AND everything that qualifies it, and `avg_score` is
# deliberately NOT selected: a sibling key can be dropped by accident in a
# renderer, a template or a copy-paste, and the number would then appear naked.
# Reading `.value` puts `.label` in the same object the caller already holds.
#
# `n_usable` is also emitted at row level because it is NOT
# `evaluated_interactions` — the old panel showed the latter next to the mean,
# which is the count of ALL evaluations including the ungradeable ones, so the
# number beside the average was not the average's denominator.
#
# `method_label` comes out because the view emits ONE ROW PER AGENT PER VERSION
# CO-ORDINATE and two of the live agents genuinely have two rows (v4-flash and
# v4-pro). Without it they read as duplicates, and somebody averages them.
# ---------------------------------------------------------------------------
# What the rubric could NOT measure, and why
#
# Module 4 nulls whenever no CHAT follow-up is on record — which is honest,
# because a phone call, a WhatsApp from the agent's own number or a walk-in are
# all invisible here and an agent who did any of them looks identical to one who
# forgot the customer. Scoring that as zero would punish whoever works the phone
# hardest for a gap in our data collection.
#
# But an honest null is still a null: `weight_applied` renormalises over the
# modules that DID apply, so the missing 20% never shows up as missing. It shows
# up as a normal-looking score. This panel is the only place it is visible, and
# it is the number to hand the client when asking for follow-up logging.
SQL_UNMEASURED = """
SELECT count(*)                                              AS evaluated,
       count(*) FILTER (WHERE e.m4_followup IS NULL)         AS followup_unmeasured,
       count(*) FILTER (WHERE e.m5_closing  IS NULL)         AS closing_not_reached,
       round(avg(e.weight_applied), 3)                       AS avg_weight_applied,
       round(avg(e.weight_applied) FILTER (WHERE e.m4_followup IS NULL), 3)
                                                             AS avg_weight_when_unmeasured
FROM agent_evaluations e
WHERE true{chat_only}
""".format(chat_only=_CHAT_ONLY)


SQL_SCORECARD = """
SELECT full_name, team, method_label, is_provisional,
       evaluated_interactions, calls, chats, n_usable,
       score_display,
       avg_reception, avg_offer, avg_objections,
       avg_followup, avg_closing, n_closing_scored,
       avg_first_response_sec, flagged_conversations
FROM v_agent_scorecard_display
ORDER BY n_usable DESC, (score_display->>'value')::numeric DESC NULLS LAST
"""

SQL_QUALITY_BY_INPUT = """
SELECT input_type, diarization, confidence_bucket, method_label, is_provisional,
       n, n_usable, score_display, score_spread
FROM v_quality_by_input_display
WHERE true{chat_only}
ORDER BY input_type, confidence_bucket
""".format(chat_only=" AND input_type = 'chat'")

# Rule 2 made visible: a module scored `null` never arose, `0` arose and was
# handled badly. If those two ever get merged again, this panel is where the
# null column collapsing to zero would show up first.
SQL_NULL_VS_ZERO = """
SELECT m.module,
       count(*) FILTER (WHERE m.score IS NULL)    AS not_applicable,
       count(*) FILTER (WHERE m.score = 0)        AS scored_zero,
       count(*) FILTER (WHERE m.score > 0)        AS scored_above_zero,
       round(avg(m.score) FILTER (WHERE m.score IS NOT NULL), 1) AS avg_when_applicable
FROM agent_evaluations e
CROSS JOIN LATERAL (VALUES
  ('m1_reception',  e.m1_reception),
  ('m2_offer',      e.m2_offer),
  ('m3_objections', e.m3_objections),
  ('m4_followup',   e.m4_followup),
  ('m5_closing',    e.m5_closing)
) AS m(module, score)
WHERE true{chat_only}
GROUP BY m.module ORDER BY m.module
""".format(chat_only=_CHAT_ONLY)

# ---------------------------------------------------------------------------
# 5 · One conversation at a time — what the judge actually said
#
# Every other panel here is an aggregate. There was no way to open a single
# conversation and read the verdict on it, which makes a low mean impossible to
# argue with: an agent shown 23.8 cannot see WHICH conversation earned it or
# what the judge objected to, and neither can the person reviewing them.
#
# NULL IS NOT ZERO, AND THIS PANEL IS WHERE IT MATTERS MOST (rule 2). A module
# is null when the situation never arose and 0 when it arose and was handled
# badly. The columns are emitted raw, exactly as stored, and the page renders
# the two differently — a null module must never appear as a zero, because that
# is the failure the whole rubric was rebuilt to remove. `weight_applied` comes
# with them so a reader can see which weights the score was actually computed
# over rather than assuming all five.
#
# NO AVERAGE IS COMPUTED HERE. `final_score` is a single observation on one
# conversation, not a mean, so it needs no sample size beside it. Averaging
# these rows into a new headline figure would reintroduce exactly the bare,
# unlabelled number that 0533b2f removed — if this panel ever needs an
# aggregate, it goes through `score_display` from the display views.
#
# WINDOWED ON THE CONVERSATION'S OWN DATE, not on when it was judged: "the last
# 30 days" means the last 30 days of business to the person reading it. When it
# was judged is a column, because a conversation from August judged yesterday
# is a normal and uninteresting thing for a backlog to do.
SQL_CONVERSATIONS = """
SELECT i.external_id,
       i.channel::text                            AS channel,
       e.input_type::text                         AS input_type,
       i.started_at,
       i.ended_at,
       i.message_count,
       i.customer_message_count,
       i.agent_message_count,
       i.external_deal_id,
       coalesce(ag.full_name, 'unassigned')       AS agent_name,

       a.summary_ar,
       a.intent,
       a.lead_temp::text                          AS lead_temp,
       a.buying_stage::text                       AS buying_stage,

       e.final_score,
       e.performance_level,
       e.weight_applied,
       e.m1_reception,
       e.m2_offer,
       e.m3_objections,
       e.m4_followup,
       e.m5_closing,
       e.top_strength,
       e.top_weakness,
       e.top_recommendation,
       e.contract_status,
       e.gradeable,
       e.model                                    AS judge_model,
       e.prompt_version,
       e.updated_at                               AS evaluated_at,

       -- The cap, carried by the rows it caps. A truncated list that does not
       -- say it is truncated reads as "these are all of them".
       count(*) OVER ()                           AS total_in_window
FROM agent_evaluations e
JOIN interactions i          ON i.interaction_id = e.interaction_id
LEFT JOIN interaction_analysis a ON a.interaction_id = e.interaction_id
LEFT JOIN agents ag          ON ag.agent_id = e.agent_id
WHERE i.started_at >= now() - make_interval(days => %(days)s){chat_only}
ORDER BY i.started_at DESC
LIMIT %(limit)s
""".format(chat_only=_CHAT_ONLY)


def _conversations(p: dict) -> dict:
    """The drill-down rows, plus how many the cap hid.

    Returns a dict rather than a bare list so `total_in_window` and
    `truncated` travel WITH the rows. A caller that renders the list cannot
    then present 50 of 800 conversations as though it were all of them.
    """
    rows = db.rows(SQL_CONVERSATIONS, p)
    total = int(rows[0]["total_in_window"]) if rows else 0
    for r in rows:
        r.pop("total_in_window", None)
    return {
        "rows": rows,
        "total_in_window": total,
        "limit": p["limit"],
        "truncated": total > len(rows),
    }


SQL_TOTALS = """
SELECT
  (SELECT count(*) FROM interactions)                             AS interactions,
  (SELECT count(*) FROM chat_messages)                            AS chat_messages,
  (SELECT count(*) FROM interaction_analysis)                     AS analysed,
  (SELECT count(*) FROM interaction_requests)                     AS requests,
  (SELECT count(*) FROM agent_evaluations)                        AS evaluations,
  (SELECT count(*) FROM model_calls)                              AS model_calls,
  (SELECT count(*) FROM customers)                                AS customers,
  (SELECT count(*) FROM deals)                                    AS deals,
  (SELECT count(*) FROM follow_ups)                               AS follow_ups,
  (SELECT count(*) FROM interactions
    WHERE customer_phone_e164 IS NULL AND customer_phone_raw IS NOT NULL)
                                                                  AS phones_unnormalised
"""

# The gap workflow 03 lives or dies on: it matches customers on the E.164 phone
# and nothing else, and the chat API never sends one. If workflow 04 has not
# backfilled phones, `with_phone` stays at zero and `customers` never grows —
# which is the failure this project already hit once and diagnosed by hand.
SQL_IDENTITY_COVERAGE = """
SELECT external_source,
       count(*)                                            AS interactions,
       count(*) FILTER (WHERE customer_phone_e164 IS NOT NULL) AS with_phone,
       count(*) FILTER (WHERE customer_id IS NOT NULL)     AS linked_to_customer,
       count(*) FILTER (WHERE deal_id IS NOT NULL)         AS linked_to_deal,
       count(*) FILTER (WHERE external_deal_id IS NOT NULL) AS carries_deal_id
FROM interactions
GROUP BY external_source ORDER BY interactions DESC
"""


# ---------------------------------------------------------------------------
# 6 · The commercial side — who came, who was serious, who bought
#
# Everything above this line grades HOW a conversation was handled. None of it
# answers the question a sales manager actually opens a dashboard with: this
# agent was given N customers, how many of them were real, and how many bought.
# That answer lived only in a hand-built page rebuilt by hand from a frozen
# snapshot, so it was out of date the moment it was published and nobody could
# tell by how much.
# ---------------------------------------------------------------------------

# WHAT MAKES AN ASK REAL, WRITTEN ONCE.
#
# A customer who names WHERE, WITH HOW MANY PEOPLE and WHEN has done the work of
# a genuine enquiry. One who asks "what have you got for Turkey" has not. That
# is the client's definition, and it is a good one precisely because it is
# mechanical: three fields are present or they are not, so it cannot drift
# between runs the way a model's opinion of "serious" does (rule 3).
#
# READ OFF `interaction_requests`, NOT `interaction_analysis`. A conversation can
# hold more than one request (rule 9) and the single-value columns on
# `interaction_analysis` describe only the PRIMARY one — a customer who asked
# about Sharm for a family in June and then about a visa would be judged on
# whichever pass 1 happened to list first. ANY request carrying all three makes
# the conversation a real ask.
#
# TWO PLACES, ONE PREDICATE, AND THE SECOND IS THE ONE THAT FIRES.
#
# The first version of this read `interaction_requests` alone, because rule 9
# says a conversation can hold more than one request and that table keeps every
# one. It returned ZERO real asks out of 46 analysed conversations, which is not
# a finding — it is an empty table. `interaction_requests` holds 2 rows for 46
# analyses: pass 1 emits `requests[]` only when it sees more than one distinct
# ask, and on this corpus it almost never does.
#
# The real number is 14, and it was in `interaction_analysis` — where nine trip
# columns had never been written by any workflow, exactly like
# `customers.display_name` and `customer_name` before 027. 029 backfilled them
# and 01d now writes them.
#
# So both branches stay. `interaction_requests` is checked first because when it
# does fire it is the more precise answer — ANY of several asks counts — and the
# primary-request columns catch the 96% of conversations it does not cover.
# Dropping either branch loses conversations silently, which is how this
# returned zero in the first place.
#
# `evidence_valid IS NOT false` is rule 9: a request whose quote was not found
# verbatim in the conversation is kept for audit and excluded from every count,
# because an invented request sends a salesperson after a customer who never
# asked. There is no equivalent flag on the primary row — pass 1 validates its
# own summary fields differently — so the second branch carries no such clause.
_REAL_ASK = """
        (EXISTS (SELECT 1 FROM interaction_requests rq
                  WHERE rq.interaction_id = i.interaction_id
                    AND rq.evidence_valid IS NOT false
                    AND nullif(btrim(rq.destination), '') IS NOT NULL
                    AND rq.travelers_total IS NOT NULL
                    AND rq.date_start      IS NOT NULL)
         OR EXISTS (SELECT 1 FROM interaction_analysis ia2
                     WHERE ia2.interaction_id = i.interaction_id
                       AND ia2.travelers_total IS NOT NULL
                       AND ia2.date_start      IS NOT NULL
                       AND EXISTS (SELECT 1 FROM interaction_destinations dd
                                    WHERE dd.analysis_id = ia2.analysis_id
                                      AND dd.role = 'destination')))
"""

# THE DENOMINATOR IS THE WHOLE POINT OF THIS PANEL.
#
# `real_asks` can only be counted on a conversation pass 1 has read, and pass 1
# has read 42 of 1,526. Publishing "2 real asks" against 35 customers would
# report 33 time-wasters where the truth is that 31 were never looked at — the
# null-is-not-zero failure of rule 2, moved out of the rubric and into a sales
# report, where it reads as a verdict on the agent.
#
# So `analysed` travels beside `real_asks` in the same row and the page renders
# the pair. An agent with nothing analysed shows no real-ask number at all,
# not a zero.
SQL_AGENT_COMMERCIAL = """
WITH scope AS (
  SELECT i.interaction_id,
         i.agent_id,
         i.customer_id,
         (ia.interaction_id IS NOT NULL)                       AS analysed,
         {real_ask}                                            AS real_ask,
         coalesce((ia.raw_response->'real_ask'->>'is_real_inquiry')::boolean,
                  false)                                       AS model_called_it_real
    FROM interactions i
    LEFT JOIN interaction_analysis ia ON ia.interaction_id = i.interaction_id
   WHERE i.agent_id IS NOT NULL
     AND i.started_at >= now() - make_interval(days => %(days)s)
),
per_agent AS (
  SELECT agent_id,
         count(*)                                              AS threads,
         count(DISTINCT customer_id) FILTER (WHERE customer_id IS NOT NULL)
                                                               AS customers,
         count(*) FILTER (WHERE analysed)                      AS analysed,
         count(*) FILTER (WHERE real_ask)                      AS real_asks,
         count(DISTINCT customer_id) FILTER (WHERE real_ask)   AS real_ask_customers,
         count(*) FILTER (WHERE model_called_it_real)          AS model_called_real
    FROM scope
   GROUP BY agent_id
),
-- Deals are NOT windowed on %(days)s. A deal opened in June and won in
-- September belongs to the agent who won it, and clipping the history to the
-- report window would make every long sale vanish from the column that exists
-- to show sales.
per_agent_deals AS (
  SELECT d.agent_id,
         count(*)                                              AS deals,
         count(*) FILTER (WHERE d.stage_semantic = 'S')        AS won,
         count(*) FILTER (WHERE d.stage_semantic = 'F')        AS lost,
         count(*) FILTER (WHERE d.stage_semantic = 'P')        AS open,
         round(sum(d.amount) FILTER (WHERE d.stage_semantic = 'S'), 2)
                                                               AS won_amount
    FROM deals d
   WHERE d.agent_id IS NOT NULL
   GROUP BY d.agent_id
)
SELECT coalesce(ag.full_name, 'unassigned')  AS agent_name,
       ag.team,
       ag.is_active,
       pa.threads,
       pa.customers,
       pa.analysed,
       pa.real_asks,
       pa.real_ask_customers,
       pa.model_called_real,
       coalesce(pd.deals, 0)                 AS deals,
       coalesce(pd.won, 0)                   AS won,
       coalesce(pd.lost, 0)                  AS lost,
       coalesce(pd.open, 0)                  AS open_deals,
       pd.won_amount
  FROM per_agent pa
  JOIN agents ag ON ag.agent_id = pa.agent_id
  LEFT JOIN per_agent_deals pd ON pd.agent_id = pa.agent_id
 -- An automation is not a salesperson and must not appear in a sales ranking.
 -- Bitrix user 1 sat top of v_agent_scorecard for a month on 1,157 turns it
 -- never sent to a customer; `is_bot` is the flag that ended that, and it is
 -- the same flag here.
 WHERE ag.is_bot IS NOT TRUE
 ORDER BY pa.customers DESC, pa.threads DESC
""".format(real_ask=_REAL_ASK)

# The same funnel without the per-agent split, so the page can carry one honest
# headline: of everyone who talked to us, how many told us enough to sell to.
SQL_REAL_ASK_FUNNEL = """
SELECT count(*)                                                 AS threads,
       count(*) FILTER (WHERE ia.interaction_id IS NOT NULL)     AS analysed,
       count(*) FILTER (WHERE {real_ask})                        AS real_asks,
       count(*) FILTER (WHERE coalesce(
         (ia.raw_response->'real_ask'->>'is_real_inquiry')::boolean, false))
                                                                 AS model_called_real,
       count(DISTINCT i.customer_id)                             AS customers
  FROM interactions i
  LEFT JOIN interaction_analysis ia ON ia.interaction_id = i.interaction_id
 WHERE i.started_at >= now() - make_interval(days => %(days)s)
""".format(real_ask=_REAL_ASK)

# THE CUSTOMER LIST — the thing this product is named after.
#
# `display_name` is filled by resolve_customer_names() (027) and travels with
# `name_source` beside it, because a name taken from the CRM record and a name a
# model read out of a conversation are not the same claim, and a page showing
# only the name cannot tell the two apart.
SQL_CUSTOMERS = """
SELECT c.customer_id::text                    AS customer_id,
       c.display_name,
       c.name_source,
       c.primary_phone_e164                   AS phone,
       c.residence_city                       AS city,
       count(DISTINCT i.interaction_id)       AS threads,
       min(i.started_at)                      AS first_at,
       max(i.started_at)                      AS last_at,
       count(DISTINCT i.interaction_id) FILTER (WHERE {real_ask})
                                              AS real_ask_threads,
       coalesce(ds.deals, 0)                  AS deals,
       coalesce(ds.won, 0)                    AS won,
       ds.won_amount,
       -- A SINGLE OBSERVATION, NOT A MEAN, AND THAT IS DELIBERATE.
       --
       -- The obvious column here is avg(final_score) per customer, and it is
       -- the wrong one. 022 exists because an agent mean shown without its
       -- denominator gets read as a verdict, and a customer has one to five
       -- conversations — so a "customer average" is a mean of one dressed up as
       -- a statistic, the same failure with a smaller n and no display view to
       -- qualify it.
       --
       -- `final_score` on ONE conversation needs no sample size beside it
       -- (panel 5 makes the same point). So: the most recent score, with the
       -- count of judged conversations next to it, and anyone who wants the
       -- spread opens the conversation list.
       (array_agg(e.final_score ORDER BY i.started_at DESC)
          FILTER (WHERE e.final_score IS NOT NULL))[1]   AS last_score,
       count(e.evaluation_id)                 AS evaluated,
       count(*) OVER ()                       AS total_in_window
  FROM customers c
  JOIN interactions i ON i.customer_id = c.customer_id
  LEFT JOIN agent_evaluations e ON e.interaction_id = i.interaction_id
  LEFT JOIN v_customer_deal_summary ds ON ds.customer_id = c.customer_id
 WHERE c.is_merged_into IS NULL
 GROUP BY c.customer_id, c.display_name, c.name_source, c.primary_phone_e164,
          c.residence_city, ds.deals, ds.won, ds.won_amount
 ORDER BY max(i.started_at) DESC
 LIMIT %(limit)s
""".format(real_ask=_REAL_ASK)

# Is the name problem fixed, and by which source? A coverage row of
# name_source = 'none' is the panel that says "027 is applied and workflow 04
# has not run yet", which otherwise looks identical to "027 did not work".
SQL_NAME_COVERAGE = """
SELECT name_source, customers, named FROM v_customer_name_coverage
"""

# ---------------------------------------------------------------------------
# 7 · Promises — the customers who are waiting
#
# `follow_ups` is materialised by workflow 03 from the promises pass 1 extracts
# ("I will send you the quote tonight"). It is the only queue in this system
# that names a person waiting for something rather than a metric.
#
# WHY IT WAS EMPTY FOR MONTHS, AND WHY THAT MATTERS FOR READING IT NOW.
# `Materialise promises` filters `i.agent_id IS NOT NULL`, and agent_id was NULL
# on almost every row until 018 — so the table sat at zero while pass 1 had been
# extracting promises the whole time. 616 of the 623 it had found were in call
# transcripts, and those rows went with the calls lane on 2026-09-14
# (ON DELETE CASCADE from `promised_in`). A small number here is therefore not a
# bug, and not evidence that agents keep their promises: it is 42 analysed
# chats.
# ---------------------------------------------------------------------------

SQL_FOLLOWUP_TOTALS = """
SELECT count(*)                                            AS promises,
       count(*) FILTER (WHERE status = 'open')             AS open,
       count(*) FILTER (WHERE status = 'fulfilled')        AS fulfilled,
       count(*) FILTER (WHERE status = 'late')             AS late,
       count(*) FILTER (WHERE status = 'missed')           AS missed,
       round(avg(hours_to_fulfil) FILTER (WHERE status IN ('fulfilled', 'late')), 1)
                                                           AS avg_hours_to_fulfil
FROM follow_ups
"""

# THE QUEUE ITSELF, WORST FIRST.
#
# 'missed' outranks 'open' because a promise past its deadline is a customer who
# has already been let down, and 'open' is one who can still be reached in time.
# Sorting by date alone buries the first kind under the second.
#
# The customer's NAME is here and not only their phone, which is most of why 027
# exists: a follow-up queue that says "+9665…" is a queue nobody works from.
SQL_FOLLOWUPS = """
SELECT f.follow_up_id::text                   AS follow_up_id,
       f.status,
       f.promise_text,
       f.promised_at,
       f.due_at,
       f.fulfilled_at,
       f.hours_to_fulfil,
       round(extract(epoch FROM (now() - coalesce(f.due_at,
             f.promised_at + interval '24 hours'))) / 3600.0, 1)
                                              AS hours_overdue,
       coalesce(ag.full_name, 'unassigned')   AS agent_name,
       c.display_name                         AS customer_name,
       c.primary_phone_e164                   AS customer_phone,
       i.external_id                          AS promised_in_external_id,
       count(*) OVER ()                       AS total_in_window
  FROM follow_ups f
  LEFT JOIN agents       ag ON ag.agent_id      = f.agent_id
  LEFT JOIN customers    c  ON c.customer_id    = f.customer_id
  LEFT JOIN interactions i  ON i.interaction_id = f.promised_in
 ORDER BY CASE f.status WHEN 'missed' THEN 0 WHEN 'late' THEN 1
                        WHEN 'open'   THEN 2 ELSE 3 END,
          f.promised_at DESC
 LIMIT %(limit)s
"""

# What people asked for, so the mix is a number rather than an impression.
SQL_SERVICE_MIX = """
SELECT coalesce(rq.service::text, 'unknown')  AS service,
       count(*)                               AS requests,
       count(DISTINCT rq.interaction_id)      AS conversations
  FROM interaction_requests rq
  JOIN interactions i ON i.interaction_id = rq.interaction_id
 WHERE rq.evidence_valid IS NOT false
   AND i.started_at >= now() - make_interval(days => %(days)s)
 GROUP BY 1
 ORDER BY requests DESC
"""


def _capped(sql: str, p: dict) -> dict:
    """A LIMITed list with the count it hid travelling beside it.

    Same contract as `_conversations`: a truncated list that does not say it is
    truncated reads as "these are all of them".
    """
    rows = db.rows(sql, p)
    total = int(rows[0]["total_in_window"]) if rows else 0
    for r in rows:
        r.pop("total_in_window", None)
    return {"rows": rows, "total_in_window": total,
            "limit": p["limit"], "truncated": total > len(rows)}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _panel(name: str, fn: Callable[[], Any], into: dict, errors: dict) -> None:
    """Run one panel. A panel that fails names its own error and does not take
    the rest of the report down with it — see the module docstring.

    `DatabaseUnavailable` is the one exception, and it propagates. A worker with
    no DATABASE_URL would otherwise answer 200 with sixteen identical panel
    errors, which reads on the page as sixteen broken queries against a working
    database. One 503 saying DATABASE_URL is not configured is the truth.
    """
    try:
        into[name] = fn()
    except db.DatabaseUnavailable:
        raise
    except Exception as exc:
        into[name] = None
        errors[name] = f"{type(exc).__name__}: {exc}"
        log.warning("report panel %r failed: %s", name, exc)


def build(days: int = 30, limit: int = SAMPLE_LIMIT) -> dict:
    """Everything the report page shows, in one round of queries."""
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    p = {"days": days, "limit": limit}

    _panel("totals", lambda: db.one(SQL_TOTALS), data, errors)

    _panel("verdicts", lambda: db.rows(SQL_VERDICTS), data, errors)
    _panel("missing_deals", lambda: db.rows(SQL_MISSING_DEALS, p), data, errors)
    _panel("unlogged_requests", lambda: db.rows(SQL_UNLOGGED_REQUESTS, p), data, errors)

    _panel("chat_jobs", lambda: db.rows(SQL_CHAT_JOBS), data, errors)
    _panel("threads_due", lambda: db.one(SQL_DUE_NOW), data, errors)
    _panel("alerts_pending", lambda: db.one(SQL_ALERTS_NOT_EVALUATED), data, errors)
    _panel("budget_gate", lambda: db.rows(SQL_BUDGET_GATE), data, errors)
    _panel("spend", lambda: db.rows(SQL_SPEND), data, errors)
    _panel("roster_gaps", lambda: db.rows(SQL_ROSTER_GAPS), data, errors)
    _panel("ingest", lambda: db.rows(SQL_INGEST_FRESHNESS), data, errors)
    _panel("identity", lambda: db.rows(SQL_IDENTITY_COVERAGE), data, errors)

    _panel("cost", lambda: db.rows(SQL_COST, p), data, errors)
    _panel("cost_by_day", lambda: db.rows(SQL_COST_BY_DAY, p), data, errors)
    _panel("judge_hours", lambda: db.rows(SQL_JUDGE_HOURS, p), data, errors)

    _panel("scorecard", lambda: db.rows(SQL_SCORECARD), data, errors)
    _panel("quality_by_input", lambda: db.rows(SQL_QUALITY_BY_INPUT), data, errors)
    _panel("null_vs_zero", lambda: db.rows(SQL_NULL_VS_ZERO), data, errors)
    _panel("unmeasured", lambda: db.one(SQL_UNMEASURED), data, errors)
    _panel("conversations", lambda: _conversations(p), data, errors)

    # 6 · the commercial side, and the people it is about
    _panel("real_ask_funnel", lambda: db.one(SQL_REAL_ASK_FUNNEL, p), data, errors)
    _panel("agent_commercial", lambda: db.rows(SQL_AGENT_COMMERCIAL, p), data, errors)
    _panel("service_mix", lambda: db.rows(SQL_SERVICE_MIX, p), data, errors)
    _panel("customers", lambda: _capped(SQL_CUSTOMERS, p), data, errors)
    _panel("name_coverage", lambda: db.rows(SQL_NAME_COVERAGE), data, errors)

    # 7 · promises
    _panel("followup_totals", lambda: db.one(SQL_FOLLOWUP_TOTALS), data, errors)
    _panel("followups", lambda: _capped(SQL_FOLLOWUPS, p), data, errors)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "sample_limit": limit,
        # Which channels this payload describes. The page prints it, and a
        # second entry here is the signal that every "chats" label on it needs
        # rewriting.
        "channels": ["chat"],
        "data": data,
        # Present and empty on a healthy report. The page renders this, so a
        # panel that silently stopped working cannot look like a panel with no
        # data in it — the two are different and only one needs fixing.
        "errors": errors,
    }
