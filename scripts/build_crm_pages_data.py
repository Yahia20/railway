#!/usr/bin/env python3
"""The six operational pages, from real data — customers, agents, team, demand.

The wireframe these pages are modelled on was drawn with invented names and
invented numbers, which is the right way to agree on a layout and the wrong
thing to hand anyone as a report. This pulls the same shapes out of
`customer360`.

WHERE IDENTITY COMES FROM. `customers` is empty because RESOLVE has never run,
so a "customer" here is a normalised phone number — the same key RESOLVE itself
matches on. That is a real identity, not a placeholder: 274 chat threads and
every call carry one. Rows say which basis they used, because a person grouped
by phone and a person with a resolved customer_id are not the same level of
certainty and a reader must be able to tell.

EVERYTHING IS READ OUT OF raw_response, NOT OFF THE TYPED COLUMNS.
`interaction_analysis` has columns for service, buying_stage, lead_temp,
budget, travellers and destinations, and all 811 rows carry the enum default:
the storage SQL in both workflows writes intent, summary_ar, confidence,
language, uncertain_fields and the payload, and nothing else. The model's
answers are all there — one level down, inside the jsonb. Reading the columns
instead would report `service = unknown` for every conversation in the company.

WHAT IS DELIBERATELY ABSENT. Ad spend, supplier cost and post-trip ratings do
not exist anywhere in this database, so no page here invents them. The gaps are
reported as gaps, with the real count of what is missing.

    railway connect postgres --tunnel-only --port 55490
    export PGPASSWORD=...
    python scripts/build_crm_pages_data.py --port 55490 \
        --out local-reports/crm_pages_data.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# The jsonb paths the two prompt families actually write. Named once so a
# future prompt version that moves a field breaks in one place.
SERVICE = "a.raw_response->>'service'"
STAGE = "a.raw_response->'commercial'->>'buying_stage'"
TEMP = "a.raw_response->'commercial'->>'lead_temperature'"
BUDGET = "(a.raw_response->'commercial'->>'budget_amount')::numeric"
NAME = "a.raw_response->'customer'->>'name'"
CITY = "a.raw_response->'customer'->>'residence_city'"
TRAVEL_TYPE = "a.raw_response->'trip'->>'travel_type'"
TRAVELLERS = "(a.raw_response->'trip'->'travelers'->>'total')::int"
REAL_ASK = "a.raw_response->'real_ask'->>'is_real_inquiry'"


def q(cur, sql, args=None):
    cur.execute(sql, args)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def jsonable(o):
    if hasattr(o, "quantize"):
        return float(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


PEOPLE = f"""
WITH conv AS (
  SELECT i.interaction_id, i.customer_phone_e164 AS phone,
         CASE WHEN i.external_source='asterisk_drive' THEN 'call' ELSE 'chat' END AS medium,
         i.external_deal_id, i.started_at, i.ended_at, i.message_count,
         {SERVICE} AS service, {STAGE} AS buying_stage, {TEMP} AS lead_temp,
         {BUDGET} AS budget_amount, {NAME} AS customer_name, {CITY} AS city,
         {TRAVEL_TYPE} AS travel_type, {TRAVELLERS} AS travellers,
         {REAL_ASK} AS real_ask,
         a.summary_ar, a.raw_response->'trip'->'destinations' AS destinations,
         e.final_score, e.contract_status, e.gradeable, d.ai_outcome
  FROM interactions i
  LEFT JOIN interaction_analysis a ON a.interaction_id = i.interaction_id
  LEFT JOIN agent_evaluations   e  ON e.interaction_id = i.interaction_id
  LEFT JOIN deals d ON d.bitrix_deal_id = i.external_deal_id
  WHERE i.customer_phone_e164 IS NOT NULL
)
SELECT phone,
       count(*)                                            AS conversations,
       count(*) FILTER (WHERE medium='chat')               AS chats,
       count(*) FILTER (WHERE medium='call')               AS calls,
       count(DISTINCT external_deal_id)
         FILTER (WHERE external_deal_id IS NOT NULL)       AS deals,
       count(*) FILTER (WHERE ai_outcome='won')            AS deals_won,
       count(*) FILTER (WHERE ai_outcome='lost')           AS deals_lost,
       count(*) FILTER (WHERE ai_outcome='no_opportunity') AS not_an_opportunity,
       count(*) FILTER (WHERE real_ask='true')             AS real_inquiries,
       min(started_at)                                     AS first_contact,
       max(coalesce(ended_at, started_at))                 AS last_contact,
       (extract(epoch FROM (now() - max(coalesce(ended_at, started_at))))/86400.0)::int AS days_silent,
       sum(message_count)                                  AS messages,
       max(budget_amount)                                  AS budget,
       round(avg(final_score) FILTER (WHERE eval_score_is_usable(
              contract_status, gradeable, final_score)), 1) AS avg_score,
       count(*) FILTER (WHERE final_score IS NOT NULL)     AS evaluated,
       (array_agg(customer_name ORDER BY started_at DESC)
          FILTER (WHERE nullif(trim(customer_name),'') IS NOT NULL))[1] AS name,
       (array_agg(city ORDER BY started_at DESC)
          FILTER (WHERE nullif(trim(city),'') IS NOT NULL))[1]          AS city,
       (array_agg(travel_type ORDER BY started_at DESC)
          FILTER (WHERE nullif(trim(travel_type),'') IS NOT NULL))[1]   AS travel_type,
       max(travellers)                                                  AS travellers,
       (array_agg(service ORDER BY started_at DESC)
          FILTER (WHERE service IS NOT NULL AND service <> 'unknown'))[1] AS service,
       (array_agg(lead_temp ORDER BY started_at DESC)
          FILTER (WHERE lead_temp IS NOT NULL AND lead_temp <> 'unknown'))[1] AS lead_temp,
       (array_agg(buying_stage ORDER BY started_at DESC)
          FILTER (WHERE buying_stage IS NOT NULL AND buying_stage <> 'unknown'))[1] AS buying_stage,
       (array_agg(summary_ar ORDER BY started_at DESC)
          FILTER (WHERE summary_ar IS NOT NULL))[1]        AS last_summary,
       (array_agg(destinations ORDER BY started_at DESC)
          FILTER (WHERE destinations IS NOT NULL))[1]      AS last_destinations
FROM conv
GROUP BY phone
ORDER BY count(*) DESC, max(coalesce(ended_at, started_at)) DESC
"""

PERSON_CONVERSATIONS = f"""
SELECT i.customer_phone_e164 AS phone,
       i.external_id, i.external_deal_id,
       CASE WHEN i.external_source='asterisk_drive' THEN 'call' ELSE 'chat' END AS medium,
       i.started_at, i.message_count,
       {SERVICE} AS service, {STAGE} AS buying_stage, {BUDGET} AS budget_amount,
       {NAME} AS customer_name, left(a.summary_ar, 240) AS summary_ar,
       e.final_score, e.performance_level, d.ai_outcome
FROM interactions i
LEFT JOIN interaction_analysis a ON a.interaction_id = i.interaction_id
LEFT JOIN agent_evaluations   e  ON e.interaction_id = i.interaction_id
LEFT JOIN deals d ON d.bitrix_deal_id = i.external_deal_id
WHERE i.customer_phone_e164 IS NOT NULL
ORDER BY i.started_at DESC
"""

DEMAND = {
    "destinations": """
        WITH d AS (
          SELECT dd->>'name' AS name, dl.ai_outcome
          FROM interaction_analysis a
          JOIN interactions i ON i.interaction_id = a.interaction_id
          LEFT JOIN deals dl ON dl.bitrix_deal_id = i.external_deal_id
          CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(a.raw_response->'trip'->'destinations')='array'
                 THEN a.raw_response->'trip'->'destinations' ELSE '[]'::jsonb END) dd
          WHERE nullif(trim(dd->>'name'),'') IS NOT NULL
        )
        SELECT name, count(*) AS inquiries,
               count(*) FILTER (WHERE ai_outcome='won')  AS won,
               count(*) FILTER (WHERE ai_outcome='lost') AS lost
        FROM d GROUP BY name ORDER BY 2 DESC LIMIT 14
    """,
    "services": f"""
        SELECT {SERVICE} AS service, count(*) AS n,
               count(*) FILTER (WHERE d.ai_outcome='won') AS won,
               round(avg({BUDGET})) AS avg_budget
        FROM interaction_analysis a
        JOIN interactions i ON i.interaction_id = a.interaction_id
        LEFT JOIN deals d ON d.bitrix_deal_id = i.external_deal_id
        GROUP BY 1 ORDER BY 2 DESC
    """,
    "group_size": f"""
        WITH t AS (SELECT {TRAVELLERS} AS n FROM interaction_analysis a)
        SELECT CASE
                 WHEN n IS NULL         THEN 'مش مذكور'
                 WHEN n = 1             THEN 'فرد'
                 WHEN n = 2             THEN 'اتنين'
                 WHEN n BETWEEN 3 AND 5 THEN 'عائلة صغيرة (3-5)'
                 ELSE 'مجموعة (6+)' END AS band,
               count(*) AS n
        FROM t GROUP BY 1 ORDER BY 2 DESC
    """,
    "stages": f"SELECT {STAGE} AS stage, count(*) AS n FROM interaction_analysis a GROUP BY 1 ORDER BY 2 DESC",
    "temps": f"SELECT {TEMP} AS temp, count(*) AS n FROM interaction_analysis a GROUP BY 1 ORDER BY 2 DESC",
    "travel_type": f"SELECT {TRAVEL_TYPE} AS travel_type, count(*) AS n FROM interaction_analysis a GROUP BY 1 ORDER BY 2 DESC",
    "cities": f"""
        SELECT {CITY} AS city, count(*) AS n FROM interaction_analysis a
        WHERE nullif(trim({CITY}),'') IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12
    """,
    "objections": """
        SELECT ob->>'kind' AS kind, count(*) AS n
        FROM interaction_analysis a
        CROSS JOIN LATERAL jsonb_array_elements(
          CASE WHEN jsonb_typeof(a.raw_response->'commercial'->'objections')='array'
               THEN a.raw_response->'commercial'->'objections' ELSE '[]'::jsonb END) ob
        WHERE nullif(trim(ob->>'kind'),'') IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC
    """,
    "outcomes": """
        SELECT ai_outcome, count(*) AS n,
               count(*) FILTER (WHERE origin='ai_derived') AS ai_derived
        FROM deals WHERE ai_outcome IS NOT NULL GROUP BY 1 ORDER BY 2 DESC
    """,
    "by_month": """
        SELECT to_char(date_trunc('month', i.started_at), 'YYYY-MM') AS month,
               count(*) FILTER (WHERE i.external_source='bitrix_chat_api') AS chats,
               count(*) FILTER (WHERE i.external_source='asterisk_drive')  AS calls
        FROM interactions i GROUP BY 1 ORDER BY 1
    """,
}

AGENTS = f"""
WITH turns AS (
  SELECT m.interaction_id, m.sender_external_id AS agent_ext, count(*) AS msgs
  FROM chat_messages m
  JOIN interactions i ON i.interaction_id = m.interaction_id
  WHERE m.sender='agent' AND m.sender_external_id IS NOT NULL
    AND i.external_source='bitrix_chat_api'
  GROUP BY 1,2
), owner AS (
  SELECT DISTINCT ON (interaction_id) interaction_id, agent_ext, msgs
  FROM turns ORDER BY interaction_id, msgs DESC, agent_ext
), gaps AS (
  SELECT t.interaction_id,
         min(extract(epoch FROM (t.sent_at - t.prev_at))) AS first_reply_secs
  FROM (
    SELECT m.interaction_id, m.sender, m.sent_at,
           lag(m.sender)  OVER w AS prev_sender,
           lag(m.sent_at) OVER w AS prev_at
    FROM chat_messages m
    JOIN interactions i ON i.interaction_id = m.interaction_id
    WHERE i.external_source='bitrix_chat_api'
    WINDOW w AS (PARTITION BY m.interaction_id ORDER BY m.sent_at, m.seq)
  ) t
  WHERE t.sender='agent' AND t.prev_sender='customer'
  GROUP BY 1
)
SELECT o.agent_ext, ag.full_name,
       count(*)                                             AS threads,
       sum(o.msgs)                                          AS messages,
       count(*) FILTER (WHERE {REAL_ASK}='true')            AS real_inquiries,
       count(*) FILTER (WHERE d.ai_outcome='won')           AS won,
       count(*) FILTER (WHERE d.ai_outcome='lost')          AS lost,
       count(*) FILTER (WHERE d.ai_outcome='no_opportunity') AS not_an_opportunity,
       count(e.evaluation_id)                               AS evaluated,
       round(avg(e.final_score) FILTER (WHERE eval_score_is_usable(
              e.contract_status, e.gradeable, e.final_score)), 1) AS avg_score,
       round(avg(e.m1_reception), 1)  AS m1,
       round(avg(e.m2_offer), 1)      AS m2,
       round(avg(e.m3_objections), 1) AS m3,
       round(avg(e.m4_followup), 1)   AS m4,
       round(avg(e.m5_closing), 1)    AS m5,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY g.first_reply_secs)/60.0)::numeric, 1)
                                                            AS median_first_reply_min
FROM owner o
JOIN interactions i              ON i.interaction_id = o.interaction_id
LEFT JOIN interaction_analysis a ON a.interaction_id = o.interaction_id
LEFT JOIN agent_evaluations   e  ON e.interaction_id = o.interaction_id
LEFT JOIN gaps g                 ON g.interaction_id = o.interaction_id
LEFT JOIN deals d                ON d.bitrix_deal_id = i.external_deal_id
LEFT JOIN agents ag              ON ag.bitrix_user_id = o.agent_ext
GROUP BY o.agent_ext, ag.full_name
ORDER BY count(*) DESC
"""

# Agent x destination, the heatmap. Only cells with real inquiries exist.
AGENT_DEST = f"""
WITH owner AS (
  SELECT DISTINCT ON (m.interaction_id) m.interaction_id, m.sender_external_id AS agent_ext
  FROM chat_messages m
  JOIN interactions i ON i.interaction_id = m.interaction_id
  WHERE m.sender='agent' AND m.sender_external_id IS NOT NULL
    AND i.external_source='bitrix_chat_api'
  ORDER BY m.interaction_id, m.sent_at
)
SELECT o.agent_ext, dd->>'name' AS destination, count(*) AS inquiries,
       count(*) FILTER (WHERE d.ai_outcome='won') AS won
FROM owner o
JOIN interactions i              ON i.interaction_id = o.interaction_id
JOIN interaction_analysis a      ON a.interaction_id = o.interaction_id
LEFT JOIN deals d                ON d.bitrix_deal_id = i.external_deal_id
CROSS JOIN LATERAL jsonb_array_elements(
  CASE WHEN jsonb_typeof(a.raw_response->'trip'->'destinations')='array'
       THEN a.raw_response->'trip'->'destinations' ELSE '[]'::jsonb END) dd
WHERE nullif(trim(dd->>'name'),'') IS NOT NULL
GROUP BY 1,2 ORDER BY 3 DESC
"""

AT_RISK = f"""
WITH last_turn AS (
  SELECT DISTINCT ON (m.interaction_id) m.interaction_id, m.sender, m.sent_at
  FROM chat_messages m
  JOIN interactions i ON i.interaction_id = m.interaction_id
  WHERE i.external_source='bitrix_chat_api'
  ORDER BY m.interaction_id, m.sent_at DESC, m.seq DESC
), owner AS (
  SELECT DISTINCT ON (m.interaction_id) m.interaction_id, m.sender_external_id
  FROM chat_messages m WHERE m.sender='agent' AND m.sender_external_id IS NOT NULL
  ORDER BY m.interaction_id, m.sent_at DESC
)
SELECT i.external_id, i.external_deal_id, i.customer_phone_e164 AS phone,
       i.message_count, lt.sent_at AS last_message_at,
       (extract(epoch FROM (now() - lt.sent_at))/86400.0)::int AS days_silent,
       {SERVICE} AS service, {BUDGET} AS budget_amount, {TEMP} AS lead_temp,
       {NAME} AS customer_name, left(a.summary_ar, 200) AS summary_ar,
       o.sender_external_id AS agent_ext, d.ai_outcome
FROM last_turn lt
JOIN interactions i              ON i.interaction_id = lt.interaction_id
LEFT JOIN interaction_analysis a ON a.interaction_id = lt.interaction_id
LEFT JOIN owner o                ON o.interaction_id = lt.interaction_id
LEFT JOIN deals d                ON d.bitrix_deal_id = i.external_deal_id
WHERE lt.sender = 'customer'
ORDER BY ((a.raw_response->'commercial'->>'budget_amount') IS NULL),
         (a.raw_response->'commercial'->>'budget_amount')::numeric DESC NULLS LAST,
         lt.sent_at
LIMIT 60
"""

# Promises the model quoted an agent making. follow_ups is empty, so this is
# the only place they exist.
PROMISES = """
SELECT i.external_id, i.customer_phone_e164 AS phone,
       CASE WHEN i.external_source='asterisk_drive' THEN 'call' ELSE 'chat' END AS medium,
       i.started_at,
       p->>'promise' AS promise, p->>'due_hint' AS due_hint,
       (extract(epoch FROM (now() - i.started_at))/86400.0)::int AS days_since
FROM interaction_analysis a
JOIN interactions i ON i.interaction_id = a.interaction_id
CROSS JOIN LATERAL jsonb_array_elements(
  CASE WHEN jsonb_typeof(a.raw_response->'promises_made_by_agent')='array'
       THEN a.raw_response->'promises_made_by_agent' ELSE '[]'::jsonb END) p
WHERE nullif(trim(p->>'promise'),'') IS NOT NULL
ORDER BY i.started_at DESC
LIMIT 80
"""

GAPS = """
SELECT
  (SELECT count(*) FROM interactions)                                        AS interactions,
  (SELECT count(*) FROM deals)                                               AS deals,
  (SELECT count(*) FROM deals WHERE amount IS NOT NULL)                      AS deals_with_amount,
  (SELECT count(*) FROM interaction_analysis
     WHERE (raw_response->'commercial'->>'budget_amount') IS NOT NULL)       AS analyses_with_budget,
  (SELECT count(*) FROM customers)                                           AS customers,
  (SELECT count(*) FROM follow_ups)                                          AS follow_ups,
  (SELECT count(*) FROM destinations)                                        AS destinations,
  (SELECT count(*) FROM interaction_metrics)                                 AS interaction_metrics,
  (SELECT count(*) FROM transcripts WHERE diarization='none')                AS mono_recordings,
  (SELECT count(*) FROM interaction_analysis)                                AS analyses,
  (SELECT count(*) FROM interactions WHERE customer_phone_e164 IS NOT NULL)  AS with_phone
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.getenv("PGPORT", "55490"))
    ap.add_argument("--out", type=Path, default=Path("local-reports/crm_pages_data.json"))
    args = ap.parse_args()
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    import psycopg
    dsn = (f"postgresql://postgres:{os.environ['PGPASSWORD']}"
           f"@127.0.0.1:{args.port}/customer360")
    out: dict = {"generated_at": datetime.now().astimezone().isoformat()}

    with psycopg.connect(dsn, connect_timeout=25) as conn, conn.cursor() as cur:
        out["people"] = q(cur, PEOPLE)
        print(f"people (by phone) : {len(out['people'])}")

        by_phone: dict = {}
        for c in q(cur, PERSON_CONVERSATIONS):
            by_phone.setdefault(c["phone"], []).append(c)
        out["person_conversations"] = by_phone
        print(f"conversations     : {sum(len(v) for v in by_phone.values())}")

        out["demand"] = {}
        for k, v in DEMAND.items():
            out["demand"][k] = q(cur, v)
            print(f"  demand.{k:14} {len(out['demand'][k])}")

        out["agents"] = q(cur, AGENTS)
        out["agent_dest"] = q(cur, AGENT_DEST)
        out["at_risk"] = q(cur, AT_RISK)
        out["promises"] = q(cur, PROMISES)
        out["gaps"] = q(cur, GAPS)[0]
        print(f"agents {len(out['agents'])} · agent×dest {len(out['agent_dest'])} · "
              f"at risk {len(out['at_risk'])} · promises {len(out['promises'])}")
        print(f"gaps: {out['gaps']}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, default=jsonable, indent=1),
                        encoding="utf-8")
    print(f"\nwrote {args.out} ({args.out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
