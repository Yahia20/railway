#!/usr/bin/env python3
"""Collect everything the review dashboard shows, into one JSON file.

Three groups, and they are different KINDS of number, which is why the page
labels each one:

  * stored   — read straight out of a column. interaction_analysis,
               agent_evaluations, the counters on interactions.
  * computed — derived here from message rows and timestamps. Nothing in the
               schema holds these yet; interaction_metrics has been empty since
               the day it was created, so every one of them is a question the
               data can already answer and nobody has asked.
  * external — from the Bitrix CRM export, for comparison only. Never merged
               with ours, because the point of showing it is that the two
               disagree.

    railway connect postgres --tunnel-only --port 55450
    export PGPASSWORD=...
    python scripts/build_dashboard_data.py --port 55450 \
        --csv DEAL_20260829_845558db_6a92a41ea74de.csv \
        --out local-reports/dashboard_data.json
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "worker"))


def q(cur, sql, args=None):
    cur.execute(sql, args)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def jsonable(o):
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "quantize"):          # Decimal
        return float(o)
    if hasattr(o, "isoformat"):         # date
        return o.isoformat()
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    return str(o)


# ---------------------------------------------------------------------------
# stored
# ---------------------------------------------------------------------------

EVALUATED = """
SELECT i.external_id, i.external_deal_id, i.channel::text AS channel,
       CASE WHEN i.external_source='asterisk_drive' THEN 'call' ELSE 'chat' END AS medium,
       i.message_count, i.customer_message_count, i.agent_message_count,
       i.started_at, i.ended_at,
       a.intent, a.service::text AS service, a.buying_stage::text AS buying_stage,
       a.lead_temp::text AS lead_temp, a.budget_amount, a.budget_currency,
       a.travelers_total, a.nights, a.summary_ar, a.confidence,
       a.raw_response->'trip'->'destinations'      AS destinations,
       a.raw_response->'commercial'->'objections'  AS objections,
       a.raw_response->'promises_made_by_agent'    AS promises,
       a.raw_response->'real_ask'->>'is_real_inquiry' AS is_real_inquiry,
       e.final_score, e.performance_level, e.weight_applied,
       e.m1_reception, e.m2_offer, e.m3_objections, e.m4_followup, e.m5_closing,
       e.top_strength, e.top_weakness, e.top_recommendation,
       e.contract_status, e.gradeable, e.model AS judge_model,
       e.prompt_version, e.created_at AS evaluated_at,
       d.ai_outcome, d.ai_budget_amount, d.origin AS deal_origin
FROM interactions i
JOIN interaction_analysis a USING (interaction_id)
LEFT JOIN agent_evaluations e USING (interaction_id)
LEFT JOIN deals d ON d.bitrix_deal_id = i.external_deal_id
WHERE i.external_source = %s
ORDER BY e.created_at DESC NULLS LAST
LIMIT %s
"""

# ---------------------------------------------------------------------------
# computed — the questions the schema can already answer
# ---------------------------------------------------------------------------

COMPUTED = {
    # Response time is the headline service metric and the column holding it
    # (first_response_seconds) is NULL on every chat row, because 01c refuses
    # to compute a rule in SQL that already lives in metrics.py — and nothing
    # else ever calls metrics.py for a stored thread.
    "response": """
        WITH turns AS (
          SELECT m.interaction_id, m.sender, m.sent_at,
                 lag(m.sender)  OVER w AS prev_sender,
                 lag(m.sent_at) OVER w AS prev_at
          FROM chat_messages m
          JOIN interactions i USING (interaction_id)
          WHERE i.external_source = 'bitrix_chat_api'
          WINDOW w AS (PARTITION BY m.interaction_id ORDER BY m.sent_at, m.seq)
        ), gaps AS (
          SELECT interaction_id,
                 extract(epoch FROM (sent_at - prev_at)) AS secs
          FROM turns
          WHERE sender = 'agent' AND prev_sender = 'customer'
        )
        SELECT count(*)                                             AS replies,
               count(DISTINCT interaction_id)                       AS threads,
               round((avg(secs)/60.0)::numeric, 1)                             AS avg_minutes,
               round(((percentile_cont(0.5) WITHIN GROUP (ORDER BY secs))/60.0)::numeric, 1) AS median_minutes,
               round(((percentile_cont(0.9) WITHIN GROUP (ORDER BY secs))/60.0)::numeric, 1) AS p90_minutes,
               count(*) FILTER (WHERE secs <= 300)                  AS under_5_min,
               count(*) FILTER (WHERE secs > 3600)                  AS over_1_hour
        FROM gaps
    """,
    # A customer who wrote last and never got an answer. Nothing in the schema
    # names this, and it is the single most actionable row in the database.
    "unanswered": """
        WITH last_turn AS (
          SELECT DISTINCT ON (m.interaction_id)
                 m.interaction_id, m.sender, m.sent_at
          FROM chat_messages m
          JOIN interactions i USING (interaction_id)
          WHERE i.external_source = 'bitrix_chat_api'
          ORDER BY m.interaction_id, m.sent_at DESC, m.seq DESC
        )
        SELECT count(*) FILTER (WHERE sender = 'customer')          AS customer_spoke_last,
               count(*)                                            AS threads,
               count(*) FILTER (WHERE sender='customer'
                                AND sent_at < now() - interval '7 days') AS silent_over_7_days
        FROM last_turn
    """,
    # The bot answers first on most threads. How long until a human arrives is
    # a different question from first_response_seconds and nothing asks it.
    "handoff": """
        WITH firsts AS (
          SELECT m.interaction_id,
                 min(m.sent_at) FILTER (WHERE m.sender='customer') AS first_customer,
                 min(m.sent_at) FILTER (WHERE m.sender='bot')      AS first_bot,
                 min(m.sent_at) FILTER (WHERE m.sender='agent')    AS first_agent
          FROM chat_messages m
          JOIN interactions i USING (interaction_id)
          WHERE i.external_source='bitrix_chat_api'
          GROUP BY m.interaction_id
        )
        SELECT count(*)                                              AS threads,
               count(*) FILTER (WHERE first_bot IS NOT NULL)         AS bot_touched,
               count(*) FILTER (WHERE first_agent IS NULL)           AS never_reached_an_agent,
               round(((percentile_cont(0.5) WITHIN GROUP (
                 ORDER BY extract(epoch FROM (first_agent - first_customer))
               ))/60.0)::numeric, 1)                                           AS median_minutes_to_human
        FROM firsts
    """,
    # after_hours is a NULL column on every row. PORTAL_TZ_OFFSET_HOURS is 3.
    "after_hours": """
        SELECT count(*)                                              AS messages,
               count(*) FILTER (WHERE extract(hour FROM (m.sent_at AT TIME ZONE 'Asia/Riyadh'))
                                      NOT BETWEEN 9 AND 21)          AS outside_9_to_21,
               count(DISTINCT m.interaction_id) FILTER (
                 WHERE m.sender='customer'
                   AND extract(hour FROM (m.sent_at AT TIME ZONE 'Asia/Riyadh'))
                       NOT BETWEEN 9 AND 21)                         AS threads_opened_after_hours
        FROM chat_messages m
        JOIN interactions i USING (interaction_id)
        WHERE i.external_source='bitrix_chat_api'
    """,
    # When the work actually arrives — staffing, not scoring.
    "by_hour": """
        SELECT extract(hour FROM (m.sent_at AT TIME ZONE 'Asia/Riyadh'))::int AS hour,
               count(*) FILTER (WHERE m.sender='customer') AS customer_messages
        FROM chat_messages m
        JOIN interactions i USING (interaction_id)
        WHERE i.external_source='bitrix_chat_api'
        GROUP BY 1 ORDER BY 1
    """,
    "by_dow": """
        SELECT to_char(m.sent_at AT TIME ZONE 'Asia/Riyadh', 'Dy') AS dow,
               extract(isodow FROM (m.sent_at AT TIME ZONE 'Asia/Riyadh'))::int AS n,
               count(*) FILTER (WHERE m.sender='customer') AS customer_messages
        FROM chat_messages m
        JOIN interactions i USING (interaction_id)
        WHERE i.external_source='bitrix_chat_api'
        GROUP BY 1,2 ORDER BY 2
    """,
    "thread_size": """
        SELECT width_bucket(message_count, 1, 101, 10) AS bucket,
               min(message_count) AS from_n, max(message_count) AS to_n, count(*) AS threads
        FROM interactions WHERE external_source='bitrix_chat_api'
        GROUP BY 1 ORDER BY 1
    """,
    # Now that the phones are imported: how many people came back.
    "repeat": """
        SELECT count(*)                                   AS people,
               count(*) FILTER (WHERE threads > 1)         AS returning,
               max(threads)                                AS most_threads
        FROM (SELECT customer_phone_e164, count(*) threads
              FROM interactions
              WHERE external_source='bitrix_chat_api' AND customer_phone_e164 IS NOT NULL
              GROUP BY 1) t
    """,
    "agents_seen": """
        SELECT m.sender_external_id AS bitrix_user_id,
               count(DISTINCT m.interaction_id) AS threads,
               count(*) AS messages
        FROM chat_messages m
        JOIN interactions i USING (interaction_id)
        WHERE i.external_source='bitrix_chat_api' AND m.sender='agent'
          AND m.sender_external_id IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12
    """,
    "content_types": """
        SELECT coalesce(m.content_type,'(null)') AS content_type, count(*) AS messages
        FROM chat_messages m JOIN interactions i USING (interaction_id)
        WHERE i.external_source='bitrix_chat_api'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 8
    """,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.getenv("PGPORT", "55450"))
    ap.add_argument("--csv", type=Path)
    ap.add_argument("--out", type=Path, default=Path("local-reports/dashboard_data.json"))
    ap.add_argument("--chats", type=int, default=40)
    ap.add_argument("--calls", type=int, default=10)
    args = ap.parse_args()
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    import psycopg
    dsn = (f"postgresql://postgres:{os.environ['PGPASSWORD']}"
           f"@127.0.0.1:{args.port}/customer360")
    out: dict = {"generated_at": datetime.utcnow().isoformat() + "Z"}

    with psycopg.connect(dsn, connect_timeout=25) as conn, conn.cursor() as cur:
        out["chats"] = q(cur, EVALUATED, ("bitrix_chat_api", args.chats))
        out["calls"] = q(cur, EVALUATED, ("asterisk_drive", args.calls))
        print(f"chats evaluated : {len(out['chats'])}")
        print(f"calls           : {len(out['calls'])}")

        out["computed"] = {}
        for name, sql in COMPUTED.items():
            rows = q(cur, sql)
            out["computed"][name] = rows[0] if len(rows) == 1 else rows
        print(f"computed blocks : {len(out['computed'])}")

        out["totals"] = q(cur, """
            SELECT
              (SELECT count(*) FROM interactions WHERE external_source='bitrix_chat_api') AS chat_threads,
              (SELECT count(*) FROM chat_messages m JOIN interactions i USING (interaction_id)
                 WHERE i.external_source='bitrix_chat_api')                               AS chat_messages,
              (SELECT count(*) FROM interactions WHERE external_source='asterisk_drive')  AS calls,
              (SELECT count(*) FROM agent_evaluations)                                    AS evaluations_all,
              (SELECT count(*) FROM agent_evaluations e JOIN interactions i USING (interaction_id)
                 WHERE i.external_source='bitrix_chat_api')                               AS chat_evaluations,
              (SELECT count(*) FROM chat_eval_jobs WHERE status='pending')                AS queue_pending,
              (SELECT count(*) FROM interactions
                 WHERE external_source='bitrix_chat_api' AND customer_phone_e164 IS NOT NULL) AS chats_with_phone,
              (SELECT count(*) FROM customers)                                            AS customers,
              (SELECT count(*) FROM deals WHERE origin='ai_derived')                      AS deals_ai,
              (SELECT count(*) FROM interaction_metrics)                                  AS interaction_metrics
        """)[0]

        # ------------------------------------------------------------------
        # external — the CRM export, matched by deal id, for comparison only
        # ------------------------------------------------------------------
        if args.csv and args.csv.exists():
            csv.field_size_limit(10 ** 7)
            with args.csv.open(encoding="utf-8-sig", newline="") as fh:
                crm = {r["ID"].strip(): r for r in csv.DictReader(fh, delimiter=";")
                       if (r.get("ID") or "").strip()}
            print(f"crm rows        : {len(crm)}")

            def num(v):
                try:
                    return float(str(v).replace(",", "").strip())
                except (TypeError, ValueError):
                    return None

            pairs = []
            for row in out["chats"]:
                c = crm.get((row.get("external_deal_id") or "").strip())
                if not c:
                    continue
                pairs.append({
                    "deal": row["external_deal_id"],
                    "external_id": row["external_id"],
                    # objective, both sides countable
                    "ours_messages": row["message_count"],
                    "crm_messages": num(c.get("Total Messages Count")),
                    "ours_customer_messages": row["customer_message_count"],
                    "crm_customer_messages": num(c.get("Customer Messages Count")),
                    "crm_bot_messages": num(c.get("Bot Messages Count")),
                    "crm_agent_messages": num(c.get("Agent Messages Count")),
                    "crm_first_response_minutes": num(c.get("First Response Time Minutes")),
                    # judgement, the two disagree on purpose
                    "ours_outcome": row.get("ai_outcome"),
                    "crm_stage": (c.get("Stage") or "").strip(),
                    "ours_lead_temp": row.get("lead_temp"),
                    "crm_lead_temp": (c.get("Lead Temperature") or "").strip(),
                    "ours_budget": float(row["budget_amount"]) if row.get("budget_amount") else None,
                    "crm_deal_value": num(c.get("Deal Potential Value SAR")),
                    "crm_quoted": num(c.get("Quoted Price SAR")),
                    "ours_service": row.get("service"),
                    "crm_request_type": (c.get("Request type TEXT") or c.get("Request type") or "").strip(),
                    "ours_travelers": row.get("travelers_total"),
                    "crm_adults": num(c.get("Adults Count")),
                    "crm_children": num(c.get("Children Count")),
                    "crm_bot_confidence": num(c.get("Bot Confidence Score")),
                    "crm_qualification": (c.get("Quality qulaifications") or "").strip(),
                    "crm_responsible": (c.get("Responsible") or "").strip(),
                })
            out["comparison"] = pairs
            print(f"comparable      : {len(pairs)}")

            fill = Counter()
            for c in crm.values():
                for k, v in c.items():
                    if (v or "").strip():
                        fill[k] += 1
            out["crm_fill"] = [{"column": k, "filled": n, "total": len(crm)}
                               for k, n in fill.most_common()]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, default=jsonable, indent=1),
                        encoding="utf-8")
    print(f"\nwrote {args.out} ({args.out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
