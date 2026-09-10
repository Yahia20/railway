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

SQL_CALL_JOBS = """
SELECT status,
       count(*)                                   AS n,
       max(updated_at)                            AS last_moved,
       count(*) FILTER (WHERE last_error IS NOT NULL) AS with_error
FROM call_ingest_jobs GROUP BY status ORDER BY n DESC
"""

# The one predicate that says whether the calls lane is whole. Rows sitting in
# 'discovered' are owned by the Modal batch; if Modal is not deployed, nothing
# claims them and they age forever while every other panel looks healthy.
SQL_STRANDED = """
SELECT count(*)                                   AS stranded,
       min(discovered_at)                         AS oldest,
       max(discovered_at)                         AS newest
FROM call_ingest_jobs
WHERE status IN ('discovered', 'asr_failed')
  AND claim_until IS NULL
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
ORDER BY input_type, confidence_bucket
"""

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
GROUP BY m.module ORDER BY m.module
"""

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
WHERE i.started_at >= now() - make_interval(days => %(days)s)
ORDER BY i.started_at DESC
LIMIT %(limit)s
"""


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
  (SELECT count(*) FROM transcripts)                              AS transcripts,
  (SELECT count(*) FROM interaction_analysis)                     AS analysed,
  (SELECT count(*) FROM interaction_requests)                     AS requests,
  (SELECT count(*) FROM agent_evaluations)                        AS evaluations,
  (SELECT count(*) FROM model_calls)                              AS model_calls,
  (SELECT count(*) FROM customers)                                AS customers,
  (SELECT count(*) FROM deals)                                    AS deals,
  (SELECT count(*) FROM follow_ups)                               AS follow_ups,
  (SELECT round(avg(asr_confidence), 3) FROM transcripts)         AS avg_asr_confidence,
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
    _panel("call_jobs", lambda: db.rows(SQL_CALL_JOBS), data, errors)
    _panel("stranded_calls", lambda: db.one(SQL_STRANDED), data, errors)
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
    _panel("conversations", lambda: _conversations(p), data, errors)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "sample_limit": limit,
        "data": data,
        # Present and empty on a healthy report. The page renders this, so a
        # panel that silently stopped working cannot look like a panel with no
        # data in it — the two are different and only one needs fixing.
        "errors": errors,
    }
